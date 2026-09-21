"""A hung backend must not stack version polls.

The home page polls `GET /TentacleHome/Version` on a fixed `setInterval` and does
not wait for the previous answer. The plugin proxies each poll to the backend
through the controller's shared `ProxyClient`, whose timeout is 15 s. When the
backend accepts the connection and then hangs, every poll is held for 15 s while
a new one arrives every 5 s: three requests in flight per open tab, on Jellyfin's
request threads and on the backend that is already struggling.

The property: whatever deadline governs the proxied version request is shorter
than the interval the client polls at, so at most one is ever outstanding.

There is no C# test host in this repo, so this reads the source the way
tests/test_frontend_state.py reads the dashboard JS.

Run from the tentacle/ directory:  python -m unittest discover -s tests
"""
import re
import unittest
from pathlib import Path

CONTROLLER = Path("../tentacle-plugin/Api/HomeScreenController.cs")
HOME_JS = Path("../tentacle-plugin/Inject/tentacle-home.js")


def _code(src: str) -> str:
    return re.sub(r"//[^\n]*", "", src)


def _poll_interval_seconds() -> float:
    js = HOME_JS.read_text()
    body = js[js.index("function startVersionPolling("):]
    body = body[: body.index("function stopVersionPolling(")]
    ms = re.findall(r"\}\s*,\s*(\d+)\s*\)\s*;", body)
    assert ms, "could not find the setInterval period"
    return int(ms[-1]) / 1000.0


def _timespan_seconds(src: str, name: str) -> float:
    m = re.search(rf"\b{name}\b\s*=\s*TimeSpan\.FromSeconds\(\s*([\d.]+)\s*\)", src)
    assert m, f"{name} is not a TimeSpan.FromSeconds constant"
    return float(m.group(1))


def _effective_deadline_seconds() -> float:
    """The shortest deadline the source applies to the version proxy call."""
    src = _code(CONTROLLER.read_text())
    client = re.search(r"ProxyClient\s*=\s*new\(\)\s*\{\s*Timeout\s*=\s*TimeSpan\.FromSeconds\(\s*([\d.]+)", src)
    assert client, "ProxyClient timeout not found"
    candidates = [float(client.group(1))]

    start = src.index("public async Task<ActionResult> GetPlaylistVersion()")
    method = src[start: src.index("[HttpGet(", start)]
    call = re.search(r"ProxyClient\.GetStringAsync\((.*?)\)\s*;", method, re.S)
    assert call, "version proxy call not found"
    args = call.group(1)
    token = re.search(r",\s*(\w+)\.Token\s*$", args.strip())
    if token:
        source = token.group(1)
        for m in re.finditer(rf"\b{source}\.CancelAfter\(\s*([^)]+?)\s*\)", method):
            arg = m.group(1)
            lit = re.match(r"TimeSpan\.FromSeconds\(\s*([\d.]+)", arg)
            candidates.append(float(lit.group(1)) if lit else _timespan_seconds(src, arg))
    return min(candidates)


class TestVersionPollDeadline(unittest.TestCase):
    def test_a_poll_cannot_outlive_the_poll_interval(self):
        interval = _poll_interval_seconds()
        deadline = _effective_deadline_seconds()
        self.assertLess(
            deadline, interval,
            f"a version poll may be held for {deadline:g}s but the client sends one "
            f"every {interval:g}s: a hung backend keeps "
            f"{int(-(-deadline // interval))} in flight per open tab")

    def test_a_timed_out_poll_still_replays_the_last_version(self):
        """#(version outage) must survive: the deadline firing is an exception like
        any other, and lands in the catch that replays the cached version."""
        src = _code(CONTROLLER.read_text())
        start = src.index("public async Task<ActionResult> GetPlaylistVersion()")
        method = src[start: src.index("[HttpGet(", start)]
        self.assertRegex(method, r"catch\s*\(\s*Exception\b")
        self.assertIn("_lastKnownVersionJson", method[method.index("catch"):])


if __name__ == "__main__":
    unittest.main()
