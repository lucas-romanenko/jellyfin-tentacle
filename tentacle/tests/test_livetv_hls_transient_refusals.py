"""The HLS worker waits out what the raw path waits out (#298, 9a1175e).

Two ways a running HLS recording ended for good although the provider was
answering again seconds later:

1. A non-standard 5xx -- 513, or Cloudflare's 520-524 -- anywhere in the
   running stream (the re-resolve at token renewal, a playlist refresh):
   the worker's own status set had only 500-504, so every other 5xx took
   the "failed fatally" branch before the budget or the recording rule was
   asked. A 5xx on a segment was skipped as a gone segment: a hole.
2. At token renewal the CHANNEL url answers 407 for a few seconds (an ended
   session, as this provider family says it). Three quick re-resolves
   (_MAX_RERESOLVE) used that up in ~3 s and ended the recording.

Now the worker classifies statuses with _raw_retryable, like the open and
the raw re-dial, and a 407 from the channel url is waited out for a
recording (not counted toward _MAX_RERESOLVE) on the refusal cap. A viewer
keeps the three-try rule for 407, and 401/403 still end a recording at once.

TENTACLE_FUZZ_SEEDS (default 200) and TENTACLE_FUZZ_FIRST (default 0) pick the
property seeds; the PR's scratch run used 2,000. Self-contained: no network.
"""
import os
import random
import unittest

import routers.livetv as livetv
from test_livetv_token_expiry import CHANNEL, Panel, _r, _stream


def _faulty(panel, faults):
    """faults: list of dicts {target: channel|playlist|chunk, status, count, after}.
    `after` matching requests pass before the fault answers `count` of them.
    The channel target only matches re-resolves (after the open's own)."""
    real = panel.route
    for f in faults:
        f.setdefault("after", 0)
        f["seen"] = 0

    def target(url):
        if url == CHANNEL:
            return "channel" if panel.issued >= 1 else None
        if url.endswith("index.m3u8"):
            return "playlist" if panel.seq >= 2 else None
        if url.endswith(".ts") and not url.endswith("black.ts"):
            return "chunk" if panel.seq >= 2 else None
        return None

    def route(url):
        t = target(url)
        for f in faults:
            if f["target"] != t or f["count"] <= 0:
                continue
            f["seen"] += 1
            if f["seen"] > f["after"]:
                f["count"] -= 1
                panel.log.append(url)
                return _r(f["status"], url)
            break
        return real(url)
    panel.route = route
    return panel


def _consecutive(got):
    return got == list(range(got[0], got[0] + len(got))) if got else True


def _no_hole(got):
    return set(got) == set(range(min(got), max(got) + 1)) if got else True


class NonStandard5xx(unittest.IsolatedAsyncioTestCase):
    async def test_a_recording_waits_out_a_513_or_52x_at_token_renewal(self):
        for status in (513, 520, 522, 524):
            panel = _faulty(Panel(ttl=3), [{"target": "channel", "status": status, "count": 1}])
            got, ended = await _stream(panel, segments=20, recording=True)
            self.assertFalse(ended, f"{status} at the re-resolve ended the recording")
            self.assertTrue(_consecutive(got), f"{status}: hole or repeat {got}")

    async def test_a_recording_waits_out_a_513_or_522_on_a_playlist_refresh(self):
        for status in (513, 522):
            panel = _faulty(Panel(ttl=50), [{"target": "playlist", "status": status, "count": 1}])
            got, ended = await _stream(panel, segments=15, recording=True)
            self.assertFalse(ended, f"{status} on a playlist refresh ended the recording")
            self.assertTrue(_consecutive(got), f"{status}: hole or repeat {got}")

    async def test_a_5xx_segment_is_fetched_again_not_skipped(self):
        panel = _faulty(Panel(ttl=50), [{"target": "chunk", "status": 513, "count": 1}])
        got, ended = await _stream(panel, segments=15, recording=True)
        self.assertFalse(ended)
        self.assertTrue(_consecutive(got), f"a 513 segment left a hole: {got}")

    async def test_a_viewer_waits_out_one_513_within_its_budget(self):
        panel = _faulty(Panel(ttl=3), [{"target": "channel", "status": 513, "count": 1}])
        got, ended = await _stream(panel, segments=20, recording=False)
        self.assertFalse(ended)

    async def test_a_509_is_waited_out_as_before(self):
        panel = _faulty(Panel(ttl=3), [{"target": "channel", "status": 509, "count": 1}])
        got, ended = await _stream(panel, segments=20, recording=True)
        self.assertFalse(ended)


class ChannelUrl407AtRenewal(unittest.IsolatedAsyncioTestCase):
    async def test_a_recording_waits_out_407_from_the_channel_url(self):
        for n in (3, 4, 6, 20):
            panel = _faulty(Panel(ttl=3), [{"target": "channel", "status": 407, "count": n}])
            got, ended = await _stream(panel, segments=20, recording=True)
            self.assertFalse(ended, f"407 x{n} from the channel url ended the recording")
            self.assertTrue(_consecutive(got), f"407 x{n}: hole or repeat {got}")

    async def test_the_wait_uses_the_refusal_cap_not_a_tight_loop(self):
        panel = _faulty(Panel(ttl=3), [{"target": "channel", "status": 407, "count": 20}])
        await _stream(panel, segments=20, recording=True)
        from test_livetv_token_expiry import SLEPT
        # once the backoff has grown, waits reach the refusal cap (15 s), never beyond
        self.assertGreater(max(SLEPT), livetv._BACKOFF_CAP)
        self.assertLessEqual(max(SLEPT), livetv._REFUSAL_BACKOFF_CAP * 1.2 + 0.01)

    async def test_a_viewer_still_ends_after_three_407_re_resolves(self):
        # Viewers keep the three-try rule: a viewer whose player has gone away
        # (a Jellyfin live stream nobody closed) must not be kept alive by it.
        panel = _faulty(Panel(ttl=3), [{"target": "channel", "status": 407, "count": 20}])
        got, ended = await _stream(panel, segments=40, recording=False)
        self.assertTrue(ended)
        self.assertLessEqual(panel.log.count(CHANNEL), 2 + livetv._MAX_RERESOLVE)

    async def test_a_recording_still_ends_when_the_login_is_refused(self):
        for status in (401, 403):
            panel = _faulty(Panel(ttl=3), [{"target": "channel", "status": status, "count": 50}])
            got, ended = await _stream(panel, segments=40, recording=True)
            self.assertTrue(ended, f"{status} from the channel url did not end the recording")

    async def test_when_the_timer_ends_the_viewer_rule_applies_again(self):
        # The recording's wait on a 407 is bounded by its timer: when Jellyfin
        # stops counting it as a recording (the lease is demoted), a channel
        # url that keeps answering 407 ends the stream after the viewer's
        # three re-resolves, not never.
        class Recording:
            def __init__(self, panel, until):
                self.panel, self.until = panel, until

            def __bool__(self):
                return self.panel.log.count(CHANNEL) < self.until

        panel = _faulty(Panel(ttl=3), [{"target": "channel", "status": 407, "count": 500}])
        got, ended = await _stream(panel, segments=200, recording=Recording(panel, until=8))
        self.assertTrue(ended, "a demoted recording kept waiting out 407 for ever")
        self.assertLessEqual(panel.log.count(CHANNEL), 8 + 2 + livetv._MAX_RERESOLVE)

    async def test_a_407_segment_re_resolves_at_once(self):
        # 407 is in _raw_retryable, but on a segment it is a token question: the
        # worker must go to the channel url, not retry the dead token in place
        # (a retry in place would get this fake's next answer and never re-resolve).
        panel = _faulty(Panel(ttl=50), [{"target": "chunk", "status": 407, "count": 1}])
        got, ended = await _stream(panel, segments=10, recording=True)
        self.assertFalse(ended)
        self.assertTrue(_consecutive(got))
        self.assertGreaterEqual(panel.issued, 2, "the 407 segment was retried in place instead of re-resolved")


# ─── property harness ───────────────────────────────────────────────────────
TRANSIENT = [407, 429, 500, 502, 503, 504, 509, 513, 520, 521, 522, 523, 524]
FATAL_LOGIN = [401, 403]


def _world(seed):
    rnd = random.Random(seed)
    recording = rnd.random() < 0.6
    faults = []
    for _ in range(rnd.randint(1, 5)):
        target = rnd.choice(["channel", "playlist", "chunk"])
        status = rnd.choice(TRANSIENT)
        if target != "channel" and status == 407:
            status = 509            # a 407 off the channel url is a token question: tested above
        faults.append({"target": target, "status": status, "count": rnd.randint(1, 6),
                       "after": rnd.randint(0, 4)})
    login = rnd.random() < 0.15
    if login:
        faults.append({"target": "channel", "status": rnd.choice(FATAL_LOGIN), "count": 99,
                       "after": rnd.randint(0, 3)})
    viewer_407_run = (not recording) and any(
        f["target"] == "channel" and f["status"] == 407 and f["count"] >= livetv._MAX_RERESOLVE for f in faults)
    return recording, faults, login, viewer_407_run


class Property(unittest.IsolatedAsyncioTestCase):
    """Invariants over random fault scripts:
    P1 a recording never ends while the provider answers only transient
       statuses (any 5xx, 429, 509, 408/425, a 407 from the channel url);
    P2 what reaches the tuner has no hole while no segment is refused for
       longer than the live window (a segment may repeat after a
       failed segment plus a re-resolve: "better a repeat than a hole", and
       main repeats in the same runs with the statuses it already waited out);
    P3 a refused login (401/403 from the channel url, for good) still ends it;
    P4 a viewer ends on transient 5xx/509 only past its budget (never here:
       the fake clock does not advance), so it ends only on P3 or a run of
       _MAX_RERESOLVE 407s from the channel url (with the token url's 407
       that started the renewal, that is one more re-resolve than allowed)."""

    async def test_random_fault_scripts(self):
        n = int(os.environ.get("TENTACLE_FUZZ_SEEDS", "200"))
        first = int(os.environ.get("TENTACLE_FUZZ_FIRST", "0"))
        for seed in range(first, first + n):
            recording, faults, login, viewer_407_run = _world(seed)
            script = [dict(f) for f in faults]
            panel = _faulty(Panel(ttl=3), faults)
            got, ended = await _stream(panel, segments=25, recording=recording)
            ctx = f"seed={seed} recording={recording} faults={script}"
            if sum(f["count"] for f in script if f["target"] == "chunk") <= 3:
                # A segment refused longer than the live window moves on is
                # gone for anyone (main loses it too); below that, never.
                self.assertTrue(_no_hole(got), f"P2 hole {got} {ctx}")
            if login:
                continue    # P3 is checked where the login refusal is reached (below)
            if recording:
                self.assertFalse(ended, f"P1 {ctx}")
            elif not viewer_407_run:
                self.assertFalse(ended, f"P4 {ctx}")

    async def test_a_refused_login_ends_it_once_reached(self):
        n = int(os.environ.get("TENTACLE_FUZZ_SEEDS", "200"))
        first = int(os.environ.get("TENTACLE_FUZZ_FIRST", "0"))
        for seed in range(first, first + n):
            rnd = random.Random(seed)
            status = rnd.choice(FATAL_LOGIN)
            before = [{"target": rnd.choice(["playlist", "chunk"]), "status": rnd.choice([509, 513, 522]),
                       "count": rnd.randint(1, 3)}]
            panel = _faulty(Panel(ttl=3), before + [{"target": "channel", "status": status, "count": 99}])
            got, ended = await _stream(panel, segments=60, recording=rnd.random() < 0.5)
            self.assertTrue(ended, f"P3 seed={seed} status={status}")


if __name__ == "__main__":
    unittest.main()
