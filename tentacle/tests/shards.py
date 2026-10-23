"""Split the suite into N shards that take about the same time, and run one.

CI runs the unit tests as 4 parallel jobs (tests.yml), and `scripts/check -j N`
runs the same shards side by side locally. A shard is a set of whole test
modules: tests/durations.json records how long each module takes, and the
modules are dealt out longest first, each to the shard with the least time so
far (a module that isn't in the file yet counts as the median). The split
depends only on the file names and durations.json, so every machine computes
the same shards, and together they are exactly the modules discover finds.

    python tests/shards.py 2/4 [-v]     run shard 2 of 4 (hermetic, as hermetic.py does)
    python tests/shards.py --list 2/4   print its modules
    python tests/shards.py 2/4.1/2      the first half of shard 2 of 4 (split the same way)
    python tests/shards.py --merge a.json b.json > tests/durations.json

With TENTACLE_DURATIONS=<file> a run also writes how long each of its modules
took (JSON); `scripts/check --durations` runs the whole suite that way and
rewrites tests/durations.json. CI records its own (artifacts "durations-K"
of a Tests run, one per shard), and CI's are the times to balance: redo the
file from a recent run when its shards drift apart:

    gh run download <run id> -p 'durations-*' -D /tmp/d
    python tests/shards.py --merge /tmp/d/*/*.json > tests/durations.json
"""
import json
import os
import statistics
import sys
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
DURATIONS = os.path.join(HERE, "durations.json")


def modules() -> list:
    return sorted(f[:-3] for f in os.listdir(HERE) if f.startswith("test") and f.endswith(".py"))


def shards(n: int, names=None) -> list:
    try:
        with open(DURATIONS) as f:
            known = json.load(f)
    except FileNotFoundError:
        known = {}
    names = modules() if names is None else names
    default = statistics.median(known.values()) if known else 1.0
    cost = {m: float(known.get(m, default)) for m in names}
    out = [[] for _ in range(n)]
    total = [0.0] * n
    for m in sorted(names, key=lambda m: (-cost[m], m)):
        i = min(range(n), key=lambda i: (total[i], i))
        out[i].append(m)
        total[i] += cost[m]
    return [sorted(s) for s in out]


def select(spec: str) -> list:
    """The modules of "K/N", or of "K/N.J/M" (part J of M of shard K of N)."""
    names = None
    for part in spec.split("."):
        try:
            k, n = (int(x) for x in part.split("/"))
        except ValueError:
            k = n = 0
        if not 1 <= k <= n:
            raise SystemExit("shards.py: shard %s: want K/N with 1 <= K <= N" % spec)
        names = shards(n, names)[k - 1]
    return names


class _TimingResult(unittest.TextTestResult):
    """Wall time per module: from its first test starting to the next module's."""

    times = {}

    def startTest(self, test):
        now = time.monotonic()
        mod = type(test).__module__
        if getattr(self, "_mod", None) != mod:
            self._close(now)
            self._mod, self._since = mod, now
        super().startTest(test)

    def stopTestRun(self):
        self._close(time.monotonic())
        super().stopTestRun()

    def _close(self, now):
        mod = getattr(self, "_mod", None)
        if mod and mod.startswith("test"):
            self.times[mod] = round(self.times.get(mod, 0.0) + now - self._since, 2)


class _TimingRunner(unittest.TextTestRunner):
    resultclass = _TimingResult


def main(argv):
    if argv[:1] == ["--list"]:
        print("\n".join(select(argv[1])))
        return
    if argv[:1] == ["--merge"]:
        times = {}
        for path in argv[1:]:
            with open(path) as f:
                times.update(json.load(f))
        print(json.dumps(times, indent=0, sort_keys=True))
        return
    if not argv:
        raise SystemExit(__doc__)
    names = select(argv[0])
    if not names:
        print("shards.py: shard %s has no test modules" % argv[0])
        return
    # as discover -s tests does: the modules under test (cwd) and the tests' own helpers
    sys.path[:0] = [HERE, os.getcwd()]
    import hermetic
    if not hermetic.LIVE:
        hermetic.install()
    record = os.environ.get("TENTACLE_DURATIONS")
    runner = _TimingRunner if record else None
    prog = unittest.main(module=None, argv=[sys.argv[0]] + argv[1:] + names, testRunner=runner, exit=False)
    if record:
        with open(record, "w") as f:
            json.dump(_TimingResult.times, f, indent=0, sort_keys=True)
    sys.exit(not prog.result.wasSuccessful())


if __name__ == "__main__":
    main(sys.argv[1:])
