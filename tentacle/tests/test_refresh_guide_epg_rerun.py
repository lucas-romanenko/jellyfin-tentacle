"""refresh-guide re-runs the EPG sync only when that can change the guide.

Run from the tentacle/ directory:  python -m unittest discover -s tests

POST /api/live/refresh-guide re-ran the whole EPG sync, inline, whenever an
enabled channel's guide id had no programmes. A tvg-id the provider's feed
does not carry is such an id after every sync, so the check was always true:
each EPG sync the dashboard starts (it calls refresh-guide when one completes)
was followed by a second full parse and rewrite of the guide, and Jellyfin's
guide refresh waited for it. On a live install 92 of 840 enabled channels
have a tvg-id with no programmes. The re-run read the same cached feed and
found nothing for them either.

It is skipped now only when the last sync already found no programmes for
every missing id, the feed it read is still the cached one, and nothing else
it reads has changed: a channel added, renamed or given another override or
tvg-id, a new guide source, guide data lost since, a sync that failed, a
restart or an expired cache all still re-run it (the fuzz test checks that
skipping never leaves an enabled channel with a guide a re-run would change).
"""
import logging
import os
import random
import shutil
import time
import unittest
from datetime import datetime, timedelta
from unittest import mock

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import models.database as mdb
import routers.livetv as livetv_router
import services.jellyfin_guide as jellyfin_guide
import services.xmltv as xmltv
from tmp_dirs import temp_dir


def setUpModule():
    logging.disable(logging.CRITICAL)


def tearDownModule():
    logging.disable(logging.NOTSET)


def _stamp(dt):
    return dt.strftime("%Y%m%d%H%M%S +0000")


_START = datetime.utcnow().replace(microsecond=0) + timedelta(hours=1)


def _feed(channels, programmes):
    """channels: [(id, display name)]; programmes: [(channel id, title)]."""
    progs = "".join(
        f'<programme start="{_stamp(_START)}" stop="{_stamp(_START + timedelta(hours=1))}" channel="{cid}">'
        f'<title>{title}</title></programme>' for cid, title in programmes)
    chans = "".join(f'<channel id="{cid}"><display-name>{name}</display-name></channel>' for cid, name in channels)
    return f"<tv>{chans}{progs}</tv>"


FEED = _feed([("AMC.ca", "AMC"), ("Extra.uk", "Something Else")],
             [("AMC.ca", "A film"), ("Extra.uk", "Extra show")])


class _World(unittest.TestCase):
    def setUp(self):
        self.tmp = temp_dir(self)
        self.addCleanup(shutil.rmtree, self.tmp, True)
        engine = create_engine(f"sqlite:///{self.tmp}/t.db")
        self.addCleanup(engine.dispose)
        mdb.Base.metadata.create_all(engine)
        self.Session = sessionmaker(bind=engine)
        self.db = self.Session()
        self.addCleanup(self.db.close)
        self.sessions = []

        def session():
            s = self.Session()
            self.sessions.append(s)
            return s

        self.addCleanup(lambda: [s.close() for s in self.sessions])
        mdb.set_setting(self.db, "jellyfin_url", "http://127.0.0.1:9")
        mdb.set_setting(self.db, "jellyfin_api_key", "k")
        self.db.commit()
        self.url = "http://192.0.2.10/xmltv.php"

        # Count every EPG sync refresh-guide starts (the real one still runs).
        self.resyncs = []
        self.counting = True
        self.run_resyncs = True
        real = livetv_router._run_epg_sync_background

        def counting(data):
            if not self.counting:
                return real(data)
            self.resyncs.append(data["id"])
            return real(data) if self.run_resyncs else True

        for target, values in ((livetv_router, {"SessionLocal": session, "_run_epg_sync_background": counting}),
                               (jellyfin_guide, {"refresh_jellyfin_guide": mock.Mock()}),
                               (xmltv, {"XMLTV_CACHE_DIR": os.path.join(self.tmp, "cache")})):
            for name, v in values.items():
                pt = mock.patch.object(target, name, v)
                pt.start()
                self.addCleanup(pt.stop)
        # What the server remembers of each provider's last sync (getattr: the
        # test also runs on a build without it, to show what it fixes).
        self.memory = getattr(livetv_router, "_epg_last_sync", {})
        for state in (self.memory, livetv_router._sync_status):
            pt = mock.patch.dict(state, clear=True)
            pt.start()
            self.addCleanup(pt.stop)
        self.jf_refresh = jellyfin_guide.refresh_jellyfin_guide

    def _provider(self, url=None):
        p = mdb.Provider(name="P", server_url="http://192.0.2.10", username="u", password="p",
                         live_tv_enabled=True, epg_url=url or self.url)
        self.db.add(p)
        self.db.commit()
        return p.id

    def _cache(self, feed, url=None):
        """The provider's XMLTV, already in the 8 h disk cache: nothing is downloaded."""
        path = xmltv._get_cache_path(url or self.url)
        before = os.path.getmtime(path) if os.path.exists(path) else 0
        with open(path, "w", encoding="utf-8") as f:
            f.write(feed)
        if os.path.getmtime(path) <= before:      # a new file, even within one clock tick
            os.utime(path, (before + 1, before + 1))
        return path

    def _sync(self, pid):
        """What "Sync EPG" on the Live TV page starts, its thread run inline; not
        counted. True when it completed."""
        class Inline:
            def __init__(self, target, args=(), daemon=None):
                self.start = lambda: target(*args)

        self.counting = False
        try:
            with mock.patch.object(livetv_router, "threading", mock.Mock(Thread=Inline)):
                livetv_router.sync_epg(pid, db=self.db)
        finally:
            self.counting = True
        return livetv_router._get_sync_status(pid).get("status") == "complete"

    def _programmes(self, channel_id):
        s = self.Session()
        try:
            return s.query(mdb.EPGProgram).filter_by(channel_id=channel_id).count()
        finally:
            s.close()


class RefreshGuideRerun(_World):
    def setUp(self):
        super().setUp()
        self.pid = self._provider()
        # AMC's tvg-id is in the feed; "Gone" carries a tvg-id the feed does
        # not have (and no name in the feed matches it); "Mystery" has none.
        for sid, name, tvg in (("1", "CA: AMC HD", "AMC.ca"), ("2", "Gone Channel", "gone.example"),
                               ("3", "Mystery", None)):
            self.db.add(mdb.LiveChannel(provider_id=self.pid, name=name, stream_id=sid, epg_channel_id=tvg,
                                        stream_url=f"http://192.0.2.10/live/{sid}.ts", enabled=True))
        self.db.commit()
        self.feed = self._cache(FEED)
        self.assertTrue(self._sync(self.pid), "the dashboard's EPG sync itself succeeds")
        self.assertEqual(1, self._programmes("AMC.ca"))
        self.assertEqual(0, self._programmes("gone.example"), "the feed has no guide for this tvg-id")

    def _refresh(self, times=1):
        app = FastAPI()
        app.include_router(livetv_router.router)
        app.dependency_overrides[mdb.get_db] = lambda: self.db
        app.dependency_overrides[livetv_router.require_admin] = lambda: None
        client = TestClient(app)
        for _ in range(times):
            r = client.post("/api/live/refresh-guide")
            self.assertEqual(200, r.status_code, r.text)
        return r.json()["message"]

    def _channel(self, sid, **values):
        self.db.query(mdb.LiveChannel).filter_by(stream_id=sid).update(values)
        self.db.commit()

    def test_a_tvg_id_the_feed_lacks_does_not_rerun_the_sync_on_every_refresh(self):
        message = self._refresh(times=3)   # the dashboard calls it after each EPG sync it starts
        self.assertEqual(3, self.jf_refresh.call_count, "Jellyfin's guide is refreshed each time")
        self.assertEqual(
            [], self.resyncs,
            f"refresh-guide re-ran the whole EPG sync {len(self.resyncs)} time(s) right after a "
            f"successful sync, only because one enabled channel's tvg-id is not in the feed")
        self.assertNotIn("synced for new channels", message)
        self.assertEqual(1, self._programmes("AMC.ca"), "the guide is untouched")

    def test_a_channel_sync_that_chains_the_guide_sync_is_not_followed_by_a_rerun(self):
        """"Save groups" / channel sync: it chains into the guide sync, then the
        dashboard calls refresh-guide. A provider type changed since the first
        sync, so only the chained sync's record can spare the re-run."""
        self.db.query(mdb.Provider).filter_by(id=self.pid).update({"provider_type": "m3u_url"})
        self.db.commit()
        p = self.db.query(mdb.Provider).filter_by(id=self.pid).one()
        self.counting = False
        livetv_router._run_channel_sync_background(livetv_router._snapshot_provider(p, "m3u_url"))
        self.counting = True
        self.assertEqual(("epg", "complete"), (livetv_router._get_sync_status(self.pid)["phase"],
                                               livetv_router._get_sync_status(self.pid)["status"]))
        self._refresh()
        self.assertEqual([], self.resyncs)

    def test_enabling_a_channel_the_feed_has_no_guide_for_does_not_rerun_it(self):
        """The sync covers disabled channels too: the one enabled since was
        already looked for."""
        self._channel("2", enabled=False)
        self.assertTrue(self._sync(self.pid))
        self._channel("2", enabled=True)
        self._refresh()
        self.assertEqual([], self.resyncs)

    def test_a_guide_id_set_since_the_sync_is_fetched(self):
        """What the re-run is for: an id the last sync never asked the feed for."""
        self.assertEqual(0, self._programmes("Extra.uk"), "no channel asked for Extra.uk yet")
        self._channel("3", epg_id_override="Extra.uk")
        self.assertIn("synced for new channels", self._refresh())
        self.assertEqual([self.pid], self.resyncs)
        self.assertEqual(1, self._programmes("Extra.uk"))

    def test_a_cleared_override_is_matched_again(self):
        """The channel falls back to a tvg-id the feed lacks (another channel's
        too, so already looked for), but its name matches a feed channel,
        which the sync never tried while the override was set."""
        self._channel("3", name="Something Else", epg_channel_id="gone.example", epg_id_override="AMC.ca")
        self.assertTrue(self._sync(self.pid))
        self._channel("3", epg_id_override=None)
        self._refresh()
        self.assertEqual([self.pid], self.resyncs)
        self.db.expire_all()
        self.assertEqual("Extra.uk", self.db.query(mdb.LiveChannel).filter_by(stream_id="3").one().guide_epg_id)

    def test_a_channel_renamed_since_the_sync_is_matched_again(self):
        """Its tvg-id is still the one the feed lacks, but its new name now
        matches a feed channel: only a re-run finds that."""
        self._channel("2", name="Something Else")
        self._refresh()
        self.assertEqual([self.pid], self.resyncs)
        self.db.expire_all()
        ch = self.db.query(mdb.LiveChannel).filter_by(stream_id="2").one()
        self.assertEqual("Extra.uk", ch.guide_epg_id)
        self.assertEqual(1, self._programmes("Extra.uk"))

    def test_a_new_guide_source_is_read(self):
        url2 = "http://192.0.2.10/other.xml"
        self._cache(_feed([("gone.example", "Gone")], [("AMC.ca", "A film"), ("gone.example", "Back")]), url2)
        self.db.query(mdb.Provider).filter_by(id=self.pid).update({"epg_url": url2})
        self.db.commit()
        self._refresh()
        self.assertEqual([self.pid], self.resyncs)
        self.assertEqual(1, self._programmes("gone.example"))

    def test_a_newer_feed_in_the_cache_is_read(self):
        self._cache(_feed([("AMC.ca", "AMC")], [("AMC.ca", "A film"), ("gone.example", "Back")]))
        self._refresh()
        self.assertEqual([self.pid], self.resyncs)
        self.assertEqual(1, self._programmes("gone.example"))

    def test_a_feed_past_the_cache_lifetime_is_downloaded_again(self):
        old = time.time() - xmltv.XMLTV_CACHE_MAX_AGE - 60
        os.utime(self.feed, (old, old))
        self.run_resyncs = False    # it would download: only the decision is checked
        self._refresh()
        self.assertEqual([self.pid], self.resyncs)

    def test_a_guide_lost_since_the_sync_is_fetched_again(self):
        self.db.query(mdb.EPGProgram).filter_by(channel_id="AMC.ca").delete()
        self.db.commit()
        self._refresh()
        self.assertEqual([self.pid], self.resyncs)
        self.assertEqual(1, self._programmes("AMC.ca"))

    def test_after_a_restart_it_runs_once_then_not_again(self):
        self.memory.clear()     # what a restart forgets
        self._refresh(times=2)
        self.assertEqual([self.pid], self.resyncs)

    def test_after_a_sync_that_failed_it_runs_again(self):
        """It failed after reading the feed, so the cached file is still the
        same one: what the earlier sync found no longer counts."""
        with mock.patch("services.epg_match.coverage_report", side_effect=RuntimeError("boom")):
            self.assertFalse(self._sync(self.pid), "the sync fails and the guide is kept")
        self.assertEqual(1, self._programmes("AMC.ca"))
        self._refresh()
        self.assertEqual([self.pid], self.resyncs)

    def test_a_channel_removed_while_the_sync_runs_does_not_fail_it(self):
        """An M3U playlist sync can remove a channel while the guide sync parses
        the feed: the guide sync still completes, as before."""
        real = xmltv.stream_parse_xmltv

        def parse(*a, **k):
            out = real(*a, **k)
            s = self.Session()
            s.query(mdb.LiveChannel).filter_by(stream_id="2").delete()
            s.commit()
            s.close()
            return out

        with mock.patch.object(xmltv, "stream_parse_xmltv", parse):
            self.assertTrue(self._sync(self.pid), livetv_router._get_sync_status(self.pid))
        self.assertEqual(1, self._programmes("AMC.ca"))


# ── Fuzz: skipping never leaves a guide a re-run would change ───────────────

_POOL = [("Bravo.ca", "Bravo"), ("Crave.ca", "Crave"), ("Dove.uk", "Dove"), ("Echo.us", "Echo"),
         ("Fox.ca", "Fox"), ("Fox.us", "FOX")]
_GONE = ["gone.example", "lost.example"]
_NAMES = [n for _, n in _POOL] + ["Gone", "Mystery", "AMC"]
_EDITS = ["none"] * 6 + ["enable", "override", "clear-override", "tvg", "rename", "add", "delete",
                         "source", "new-feed", "lost-guide", "restart", "failed-sync"]


def _random_feed(rnd):
    chans = [("AMC.ca", "AMC")] + [c for c in _POOL if rnd.random() < 0.7]
    progs = [("AMC.ca", "A film")] + [(cid, f"Show {cid}") for cid, _ in chans[1:] if rnd.random() < 0.7]
    return _feed(chans, progs)


def _random_channel(rnd, ids):
    return {"name": rnd.choice(["CA: ", "UK: ", "US: ", ""]) + rnd.choice(_NAMES),
            "epg_channel_id": rnd.choice([None, None] + _GONE + ids),
            "epg_id_override": rnd.choice(ids + _GONE) if rnd.random() < 0.15 else None,
            "enabled": rnd.random() < 0.7}


class RefreshGuideRerunFuzz(_World):
    """For a random lineup and feed, one random edit after a successful sync.
    Whenever main would re-run the sync (an enabled channel's guide id has no
    programmes), the enabled channels' guide after refresh-guide must be what
    a forced re-run gives (P1: skipping never loses anything a re-run would
    bring), and with nothing edited it does not re-run (P2: the bug)."""

    def _enabled_guide(self):
        s = self.Session()
        try:
            chans = s.query(mdb.LiveChannel).filter_by(enabled=True).all()
            ids = {c.guide_epg_id for c in chans} - {None}
            progs = sorted((p.channel_id, p.start, p.title) for p in s.query(mdb.EPGProgram) if p.channel_id in ids)
            missing = ids - {p[0] for p in progs}
            return sorted((c.id, c.guide_epg_id or "") for c in chans), progs, missing
        finally:
            s.close()

    def _edit(self, rnd, pid, edit):
        ids = [cid for cid, _ in _POOL] + ["AMC.ca", "Extra.new"]
        chans = self.db.query(mdb.LiveChannel).filter(mdb.LiveChannel.provider_id == pid,
                                                      mdb.LiveChannel.stream_id != "0").all()
        ch = rnd.choice(chans)
        if edit == "enable":
            ch.enabled = not ch.enabled
        elif edit == "override":
            ch.epg_id_override = rnd.choice(ids + _GONE)
        elif edit == "clear-override":
            ch.epg_id_override = None
        elif edit == "tvg":
            ch.epg_channel_id = rnd.choice([None] + ids + _GONE)
        elif edit == "rename":
            ch.name = rnd.choice(["CA: ", "UK: ", ""]) + rnd.choice(_NAMES)
        elif edit == "add":
            self.db.add(mdb.LiveChannel(provider_id=pid, stream_id="new", stream_url="http://192.0.2.10/n.ts",
                                        **_random_channel(rnd, ids)))
        elif edit == "delete":
            self.db.delete(ch)
        elif edit == "source":
            url2 = "http://192.0.2.10/other.xml"
            self._cache(_random_feed(rnd), url2)
            self.db.query(mdb.Provider).filter_by(id=pid).update({"epg_url": url2})
        elif edit == "new-feed":
            self._cache(_random_feed(rnd))
        elif edit == "lost-guide":
            gone = rnd.choice([p.channel_id for p in self.db.query(mdb.EPGProgram)])
            self.db.query(mdb.EPGProgram).filter_by(channel_id=gone).delete()
        elif edit == "restart":
            self.memory.clear()
        elif edit == "failed-sync":
            with open(xmltv._get_cache_path(self.url), encoding="utf-8") as f:
                feed = f.read()
            with mock.patch.object(xmltv, "stream_parse_xmltv", return_value=[]), mock.patch("time.sleep"):
                self.assertFalse(self._sync(pid))
            self._cache(feed)
        self.db.commit()

    def test_random_edits_never_skip_a_rerun_that_would_change_the_guide(self):
        n = int(os.environ.get("TENTACLE_FUZZ_SEEDS", "40"))
        first = int(os.environ.get("TENTACLE_FUZZ_FIRST", "0"))
        skipped = 0
        for seed in range(first, first + n):
            rnd = random.Random(seed)
            for model in (mdb.EPGProgram, mdb.LiveChannel, mdb.Provider):
                self.db.query(model).delete()
            self.db.commit()
            self.memory.clear()
            pid = self._provider()
            self._cache(_random_feed(rnd))
            ids = [cid for cid, _ in _POOL] + ["AMC.ca"]
            self.db.add(mdb.LiveChannel(provider_id=pid, name="CA: AMC HD", stream_id="0", epg_channel_id="AMC.ca",
                                        stream_url="http://192.0.2.10/0.ts", enabled=True))
            for i in range(rnd.randint(2, 6)):
                self.db.add(mdb.LiveChannel(provider_id=pid, stream_id=str(i + 1),
                                            stream_url=f"http://192.0.2.10/{i + 1}.ts", **_random_channel(rnd, ids)))
            self.db.commit()
            self.assertTrue(self._sync(pid), f"seed={seed}")

            edit = rnd.choice(_EDITS)
            self._edit(rnd, pid, edit)
            main_reruns = bool(self._enabled_guide()[2])
            before = len(self.resyncs)
            livetv_router.refresh_jellyfin_guide(db=self.db)
            reran = len(self.resyncs) > before
            got = self._enabled_guide()[:2]
            self.assertTrue(self._sync(pid), f"seed={seed}")        # the oracle: a forced re-run
            ctx = f"seed={seed} edit={edit} reran={reran}"
            if main_reruns:
                self.assertEqual(self._enabled_guide()[:2], got, f"P1 {ctx}")
                skipped += not reran
            if edit == "none":
                self.assertFalse(reran, f"P2 {ctx}")
        if n >= 20:
            self.assertGreater(skipped, 0, "no seed exercised a skipped re-run")


if __name__ == "__main__":
    unittest.main()
