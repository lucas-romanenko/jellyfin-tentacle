"""Randomised fault-injection property test for the series rating restore.

Jellyfin 10.11.8's ItemUpdate copies a Series' OfficialRating (unless the child
locked it) and CustomRating onto every Season and Episode, and a Season's onto
its Episodes. JellyfinService.set_item_tags/_post_cascading_update snapshot the
children's own ratings, save them to <DATA_DIR>/pending_rating_restores.json,
update the series, and write the own ratings back; retry_pending_rating_restores
finishes what a failure left. This test drives that code against a model of
Jellyfin with those exact cascade rules, and at EVERY Jellyfin call injects,
at random: success, a 500, a timeout, "applied but the reply was lost", or a
process restart (all state but the pending file on disk is gone). Between
steps it runs Refresh-Tags / nightly passes (sometimes two at once), makes
hand edits, and now and then corrupts the pending file.

Invariants, checked after every step:
  I1  once a fault-free settle phase has run and nothing is pending, every
      child with a rating of its own has it (hand edits count as own);
  I2  at any moment, a child whose visible rating differs from its own is in
      the pending file — except (a) the documented listing-failure fallback,
      which must have written its "rating_cascade_unprotected" Activity entry,
      (b) a restore the retry cap gave up on, which must have written
      "rating_restore_failed", and (c) entries a corrupted pending file lost,
      which must have been reported and kept as a .corrupt copy;
  I3  a hand edit is never overwritten by Tentacle;
  I4  no push is blocked for good: after the faults stop, a push succeeds.

Deterministic per seed. Concurrent passes are modelled the way the code's
own lock serialises them (the threaded interleaving itself is covered by
test_series_push_keeps_episode_ratings). A failing seed is printed; keep it
in REGRESSION_SEEDS.

Run from tentacle/:  python -m unittest discover -s tests -p "test_rating_restore_property.py"
Env: RATING_PROPERTY_SEEDS (default 500), RATING_PROPERTY_FIRST_SEED (default 1),
RATING_PROPERTY_STEPS (default 25 steps per seed).
"""
import json
import os
import random
import shutil
import tempfile
import threading
import unittest
from unittest import mock

import requests

RATINGS = ["TV-Y", "TV-Y7", "TV-G", "TV-PG", "TV-14", "TV-MA"]
CUSTOMS = ["C-KIDS", "C-TEEN", "C-ADULT"]
# Seeds that found a violation on 89d2b79 (583 of seeds 1-3000 broke I2 and 39
# broke I1 there): hand edits lost to a pending entry's old value, to a season
# restore's cascade, to a restart between that cascade and the next save, or
# on a field the snapshot had as "inherits"; a partial hand edit dropping the
# other field's restore; a season's copy recorded as its own. Always run.
REGRESSION_SEEDS = [1, 2, 9, 13, 17, 18, 119, 176, 331, 387]


class _Restart(BaseException):
    """The Tentacle process died mid-call. BaseException, so no `except
    Exception` in the code under test can swallow it — as with a real crash."""


class Model:
    """Jellyfin's items and the ItemUpdate cascade of 10.11.8."""

    def __init__(self, rng):
        self.items = {}
        self.items["s1"] = {"Id": "s1", "Type": "Series", "Name": "Show", "ParentId": None,
                            "Tags": ["Netflix TV"], "OfficialRating": rng.choice(RATINGS),
                            "CustomRating": rng.choice([None, None, None] + CUSTOMS),
                            "LockedFields": []}
        for si in range(1, rng.randint(1, 3) + 1):
            sid = f"se{si}"
            self.items[sid] = self._child(rng, sid, "Season", "s1", si, None)
            for ei in range(1, rng.randint(1, 4) + 1):
                eid = f"e{si}{ei}"
                self.items[eid] = self._child(rng, eid, "Episode", sid, ei, si)
        # What each child "should" carry: its own rating (None = inherits).
        self.own = {k: (v["OfficialRating"], v["CustomRating"])
                    for k, v in self.items.items() if k != "s1"}

    @staticmethod
    def _child(rng, cid, ctype, parent, index, season_index):
        return {"Id": cid, "Type": ctype, "ParentId": parent, "IndexNumber": index,
                "ParentIndexNumber": season_index,
                "OfficialRating": rng.choice([None, None] + RATINGS),
                "CustomRating": rng.choice([None, None, None, None] + CUSTOMS),
                "LockedFields": ["OfficialRating"] if rng.random() < 0.1 else [],
                "Tags": []}

    def children(self, pid):
        out = []
        for it in self.items.values():
            p = it.get("ParentId")
            while p and p != pid:
                p = self.items[p].get("ParentId")
            if p == pid:
                out.append(it)
        return out

    def dto(self, it):
        d = {k: (list(v) if isinstance(v, list) else v) for k, v in it.items()}
        if d["Type"] == "Episode":
            d["SeasonId"] = d["ParentId"]
        return d

    def apply_update(self, item_id, body):
        it = self.items[item_id]
        official = (body.get("OfficialRating") or "").strip() or None
        custom = body.get("CustomRating")
        it["Tags"] = list(body.get("Tags") or [])
        it["OfficialRating"], it["CustomRating"] = official, custom
        if it["Type"] in ("Series", "Season"):
            for child in self.children(item_id):
                if it["Type"] == "Season" and child["Type"] != "Episode":
                    continue
                if "OfficialRating" not in child["LockedFields"]:
                    child["OfficialRating"] = official
                child["CustomRating"] = custom

    def visible(self, cid):
        it = self.items[cid]
        return (it["OfficialRating"], it["CustomRating"])


class Faults:
    """Decides the outcome of each Jellyfin call, per thread, from the seed."""

    def __init__(self, seed):
        self.seed = seed
        self.rngs = {}
        self.enabled = True
        self.dead = False
        self.p = None

    def rng(self):
        name = threading.current_thread().name
        if name not in self.rngs:
            self.rngs[name] = random.Random(f"{self.seed}:{name}")
        return self.rngs[name]

    def outcome(self):
        if self.dead:
            raise _Restart()
        if not self.enabled:
            return "ok"
        r = self.rng().random()
        p = self.p
        for kind in ("500", "timeout", "lost", "restart"):
            if r < p[kind]:
                if kind == "restart":
                    self.dead = True
                    raise _Restart()
                return kind
            r -= p[kind]
        return "ok"


class FakeJellyfin:
    """JellyfinService's transport, backed by the Model and the Faults."""

    def __init__(self, model, faults):
        self.model, self.faults = model, faults
        self.on_first_call = None
        self.after_series_update = None

    def _hook(self):
        if self.on_first_call:
            hook, self.on_first_call = self.on_first_call, None
            hook()

    def get(self, path, params=None):
        self._hook()
        kind = self.faults.outcome()
        if kind == "500":
            raise requests.HTTPError("500 Server Error (injected)")
        if kind in ("timeout", "lost"):
            return None                       # JellyfinService._get turns a timeout into None
        params = params or {}
        m = self.model
        if path == "/Items" and "Ids" in params:
            it = m.items.get(params["Ids"])
            return {"Items": [m.dto(it)] if it else [], "TotalRecordCount": 1 if it else 0}
        if path == "/Items":
            kids = [k for k in m.children(params["ParentId"])
                    if k["Type"] in params["IncludeItemTypes"].split(",")]
            start, limit = params["StartIndex"], params["Limit"]
            return {"Items": [m.dto(k) for k in kids[start:start + limit]], "TotalRecordCount": len(kids)}
        return m.dto(m.items[path.rsplit("/", 1)[-1]])

    def _write(self, item_id, body):
        self._hook()
        kind = self.faults.outcome()
        if kind == "500":
            return 500
        if kind == "timeout":
            raise requests.ConnectionError("timed out (injected)")
        self.model.apply_update(item_id, body)
        if self.after_series_update and self.model.items[item_id]["Type"] == "Series":
            self.after_series_update()
        if kind == "lost":
            raise requests.ConnectionError("reply lost after the update was applied (injected)")
        return 204

    def session_post(self, url, json=None, timeout=None):
        status = self._write(url.rsplit("/", 1)[-1], json)
        return mock.Mock(status_code=status, text="" if status < 400 else "Internal Server Error")

    def post(self, path, data=None):         # JellyfinService._post
        status = self._write(path.rsplit("/", 1)[-1], data)
        if status >= 400:
            raise requests.HTTPError(f"{status} Server Error (injected)")
        return True


def _service(fake):
    from services.jellyfin import JellyfinService
    jf = JellyfinService("http://jf.invalid:8096", "k", "u1")
    jf._get = fake.get
    jf._post = fake.post
    jf.session = mock.Mock()
    jf.session.post.side_effect = fake.session_post
    return jf


class Scenario:
    def __init__(self, seed, data_dir):
        self.seed = seed
        self.rng = random.Random(seed)
        self.data_dir = data_dir
        self.model = Model(self.rng)
        self.faults = Faults(seed)
        self.faults.p = {"500": self.rng.uniform(0, 0.12), "timeout": self.rng.uniform(0, 0.12),
                         "lost": self.rng.uniform(0, 0.12), "restart": self.rng.uniform(0, 0.04)}
        self.fake = FakeJellyfin(self.model, self.faults)
        self.activity = []
        self.exempt = {}           # child -> why (a documented, reported loss of its own rating)
        self.exempt_fields = {}    # child -> the fields that loss covers
        self.hand = {}             # child -> the value the user set
        self.corrupted = False
        self.tag_n = 0
        self.log = []

    # ── helpers ────────────────────────────────────────────────────────────
    def pending(self):
        import services.jellyfin as j
        try:
            data = json.loads(j._pending_restores_path().read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def pending_children(self):
        e = self.pending().get("s1")
        return set((e or {}).get("children", {}) if isinstance(e, dict) else ())

    def fields(self, cid):
        """The fields (0 = OfficialRating, 1 = CustomRating) where the child
        shows something other than its own value, less those a documented,
        reported loss has exempted."""
        own, vis = self.model.own[cid], self.model.visible(cid)
        return {i for i, (o, v) in enumerate(zip(own, vis)) if o is not None and o != v} \
            - self.exempt_fields.get(cid, set())

    def exempt_child(self, cid, why):
        own, vis = self.model.own[cid], self.model.visible(cid)
        lost = {i for i, (o, v) in enumerate(zip(own, vis)) if o is not None and o != v}
        if lost:
            self.exempt_fields.setdefault(cid, set()).update(lost)
            self.exempt.setdefault(cid, why)

    def differs(self, cid):
        return bool(self.fields(cid))

    def fail(self, msg):
        raise AssertionError(f"seed {self.seed}: {msg}\n  steps: {self.log[-12:]}\n"
                             f"  own={self.model.own}\n  visible="
                             f"{ {k: self.model.visible(k) for k in self.model.own} }\n"
                             f"  pending={self.pending()}\n  activity={self.activity}")

    # ── steps ──────────────────────────────────────────────────────────────
    def push(self, jf=None):
        """A Refresh Tags / nightly pass: retry pending, then push new tags."""
        import services.jellyfin as j
        jf = jf or _service(self.fake)
        try:
            j._retry_pending_rating_restores(jf, "prop")
            self.tag_n += 1
            cur = list(self.model.items["s1"]["Tags"])
            jf.set_item_tags("s1", cur + [f"T{self.tag_n}"])
        except _Restart:
            self.log.append("restart")
            self.faults.dead = False
        except requests.HTTPError:
            pass                                      # a 500 surfaced to the caller: the caller logs it

    def on_activity(self, event, message):
        """The Activity hook: a documented, reported loss exempts its children."""
        import re
        self.activity.append((event, message))
        if event == "rating_restore_failed":
            for cid in re.findall(r"(\w+) \([^)]*\)", message.split("by hand in Jellyfin:", 1)[-1]):
                if cid in self.model.own:
                    self.exempt_child(cid, "retry cap gave up")
        if event == "rating_cascade_unprotected":
            # Logged just before the unprotected update: whatever that update
            # flattens is the reported loss.
            def mark():
                self.fake.after_series_update = None
                for cid in self.model.own:
                    self.exempt_child(cid, "listing-failure fallback")
            self.fake.after_series_update = mark

    def concurrent_push(self):
        """Two passes at once (Refresh Tags beside the nightly push). The code
        serialises the cascading part under _CASCADE_LOCK (proven by a
        threaded test in test_series_push_keeps_episode_ratings), so the
        faithful — and deterministic — model is: B reads the series before A
        runs (a stale item, as B's GET happens outside the lock), A's whole
        pass, then B's retry and update with that stale item."""
        import services.jellyfin as j
        jf_b = _service(self.fake)
        try:
            item_b = jf_b._get(jf_b._item_path("s1"))
        except _Restart:
            self.faults.dead = False
            item_b = None
        except requests.HTTPError:
            item_b = None
        self.push()
        try:
            j._retry_pending_rating_restores(jf_b, "prop-B")
            if item_b:
                self.tag_n += 1
                payload = j._item_update_payload(item_b, Tags=list(item_b["Tags"]) + [f"T{self.tag_n}"])
                jf_b._post_item_update(item_b, payload, "set tags on")
        except _Restart:
            self.log.append("restart")
            self.faults.dead = False
        except requests.HTTPError:
            pass

    def hand_edit(self):
        """The user sets an episode's rating in Jellyfin (not to a value a cascade
        could have written, which would be indistinguishable from a copy)."""
        # Episodes only: a season edited in Jellyfin's UI is itself an
        # ItemUpdate, whose cascade (the user's own action) re-rates its episodes.
        cid = self.rng.choice(sorted(k for k in self.model.own if self.model.items[k]["Type"] == "Episode"))
        it = self.model.items[cid]
        series = self.model.items["s1"]
        season = self.model.items.get(it["ParentId"]) if it["Type"] == "Episode" else None
        copies = {series["OfficialRating"]}
        if season:
            copies.add(season["OfficialRating"])
            copies.add(self.model.own.get(season["Id"], (None, None))[0])
        e = self.pending().get("s1") if isinstance(self.pending().get("s1"), dict) else None
        for c in (e or {}).get("children", {}).values() if e else ():
            if isinstance(c, dict):
                copies.add(c.get("official"))
                copies.update((c.get("season_rating") or [None, None])[:1])
        for key in ("copy", "prev_copy"):
            if e and isinstance(e.get(key), list):
                copies.add(e[key][0])
        choices = [r for r in RATINGS if r not in copies]
        if not choices or "OfficialRating" in it["LockedFields"]:
            return
        value = self.rng.choice(choices)
        it["OfficialRating"] = value                   # a UI edit of one episode: no cascade
        self.model.own[cid] = (value, self.model.own[cid][1])
        self.hand[cid] = value
        # The edit makes the rating field right again; a lost custom rating
        # stays lost.
        self.exempt_fields.get(cid, set()).discard(0)
        self.log.append(f"hand {cid}={value}")

    def corrupt(self):
        import services.jellyfin as j
        path = j._pending_restores_path()
        lost = self.pending_children()
        path.write_text(self.rng.choice(['{"s1": {"children": {"e1', '[1, 2]', 'null', '{"s1": 5}']),
                        encoding="utf-8")
        self.corrupted = True
        for cid in lost:
            self.exempt_child(cid, "corrupt pending file")
        self.log.append("corrupt")

    # ── invariants ─────────────────────────────────────────────────────────
    def check_step(self):
        pend = self.pending_children()
        for cid in self.model.own:
            if self.differs(cid) and cid not in pend:
                self.fail(f"I2: {cid} shows {self.model.visible(cid)} but its own is "
                          f"{self.model.own[cid]} and nothing is pending for it")
        for cid, value in self.hand.items():
            vis = self.model.visible(cid)[0]
            if vis != value and cid not in pend and 0 not in self.exempt_fields.get(cid, set()):
                self.fail(f"I3: hand edit {cid}={value} was overwritten with {vis}")
        for cid, why in self.exempt.items():
            need = {"listing-failure fallback": "rating_cascade_unprotected",
                    "retry cap gave up": "rating_restore_failed"}.get(why)
            if need and not any(e == need for e, _ in self.activity):
                self.fail(f"{cid} lost its rating ({why}) without a {need} Activity entry")
        if self.corrupted:
            import services.jellyfin as j
            j._pending_restores_load()                 # a corrupt file is reported and kept aside
            if not any(n.startswith("pending_rating_restores.json.corrupt-")
                       for n in os.listdir(self.data_dir)) and self.pending_file_was_bad():
                self.fail("a corrupt pending file was not kept as a .corrupt copy")

    def pending_file_was_bad(self):
        import services.jellyfin as j
        try:
            json.loads(j._pending_restores_path().read_text(encoding="utf-8"))
            return False
        except (OSError, ValueError):
            return True

    def settle(self):
        """Faults stop; one pass must succeed (I4) and leave everything right (I1)."""
        self.faults.enabled = False
        self.faults.dead = False
        jf = _service(self.fake)
        import services.jellyfin as j
        j._retry_pending_rating_restores(jf, "prop")
        self.tag_n += 1
        want = list(self.model.items["s1"]["Tags"]) + [f"T{self.tag_n}"]
        if not jf.set_item_tags("s1", want):
            self.fail("I4: a push with no faults did not succeed")
        if self.model.items["s1"]["Tags"] != want:
            self.fail("I4: the tags did not land")
        if self.pending_children():
            self.fail(f"I1: still pending after a fault-free pass: {sorted(self.pending_children())}")
        for cid in self.model.own:
            if self.differs(cid):
                self.fail(f"I1: {cid} settled at {self.model.visible(cid)}, own {self.model.own[cid]}")
        self.check_step()

    def run(self, steps):
        for _ in range(steps):
            r = self.rng.random()
            if r < 0.55:
                self.log.append("push")
                self.push()
            elif r < 0.70:
                self.log.append("concurrent")
                self.concurrent_push()
            elif r < 0.90:
                self.hand_edit()
            elif r < 0.93:
                self.corrupt()
            else:
                self.log.append("retry")
                import services.jellyfin as j
                try:
                    j._retry_pending_rating_restores(_service(self.fake), "prop")
                except _Restart:
                    self.faults.dead = False
            self.check_step()
        self.settle()


def _run_seed(seed, steps=None):
    steps = steps or int(os.environ.get("RATING_PROPERTY_STEPS", "25"))
    import services.jellyfin as j
    data_dir = tempfile.mkdtemp(prefix=f"rating-prop-{seed}-")
    sc = Scenario(seed, data_dir)
    try:
        with mock.patch.dict(os.environ, {"DATA_DIR": data_dir}), \
                mock.patch.object(j, "IN_RUN_RETRY_DELAY", 0), \
                mock.patch.object(j, "CASCADE_LOCK_TIMEOUT", 20), \
                mock.patch.object(j, "_log_activity_safe", side_effect=sc.on_activity), \
                mock.patch.object(j.logger, "warning"), mock.patch.object(j.logger, "error"), \
                mock.patch.object(j.logger, "info"):
            sc.run(steps)
    finally:
        shutil.rmtree(data_dir, ignore_errors=True)


class TestRatingRestoreProperties(unittest.TestCase):
    def test_regression_seeds(self):
        for seed in REGRESSION_SEEDS:
            with self.subTest(seed=seed):
                _run_seed(seed)

    def test_random_fault_injection(self):
        first = int(os.environ.get("RATING_PROPERTY_FIRST_SEED", "1"))
        n = int(os.environ.get("RATING_PROPERTY_SEEDS", "500"))
        failures = []
        for seed in range(first, first + n):
            try:
                _run_seed(seed)
            except AssertionError as e:
                failures.append(str(e))
                if len(failures) >= 3:
                    break
        if failures:
            self.fail(f"{len(failures)} seed(s) violated an invariant:\n\n" + "\n\n".join(failures))


if __name__ == "__main__":
    unittest.main()
