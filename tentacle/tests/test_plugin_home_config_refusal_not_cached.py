"""A refused home-config fetch must not be cached under the user's key.

`HomeScreenManager.GetHomeConfig` caches whatever `FetchFromApi` returned under
the *user's* id -- including the null it returns when the backend answered 401
or 403. But a 401/403 is a verdict on the caller's token, not on the user's
config: one request carrying an expired or foreign token stores "no config" for
that user, and the user's own validly-authenticated requests are then served
the cached null (empty toolbar, rows without their sort settings) until the
entry expires. Found live in phase 3 as the second half of the #61 toolbar bug.

There is no C# test host in this repo, so this reads the source the way
tests/test_frontend_state.py reads the dashboard JS.

Run from the tentacle/ directory:  python -m unittest discover -s tests
"""
import re
import unittest
from pathlib import Path

MANAGER = Path("../tentacle-plugin/HomeScreen/HomeScreenManager.cs")


def _strip_comments(src: str) -> str:
    return re.sub(r"//[^\n]*", "", src)


def _method(src: str, signature_start: str) -> str:
    start = src.index(signature_start)
    rest = src[start:]
    end = re.search(r"\n    /// <summary>|\n    private |\n    public ", rest[1:])
    return rest[: end.start() + 1] if end else rest


def _enclosing_conditions(body: str, needle: str):
    """The `if (...)` headers of every block that encloses `needle`."""
    pos = body.index(needle)
    conditions, stack = [], []
    for m in re.finditer(r"[{}]", body[:pos]):
        if m.group() == "{":
            header = body[:m.start()].rstrip().splitlines()[-1].strip()
            stack.append(header)
        else:
            stack.pop()
    return [h for h in stack if h.startswith("if")]


class TestRefusalIsNotCached(unittest.TestCase):
    def setUp(self):
        self.src = _strip_comments(MANAGER.read_text())
        self.get = _method(self.src, "public HomeConfig? GetHomeConfig(")
        self.fetch = _method(self.src, "private HomeConfig? FetchFromApi(")

    def test_the_fetch_tells_a_refusal_apart_from_other_failures(self):
        failure = self.fetch[self.fetch.index("IsSuccessStatusCode"):]
        self.assertRegex(failure, r"Unauthorized|\b401\b",
                         "FetchFromApi treats a 401 like any other failure")
        self.assertRegex(failure, r"Forbidden|\b403\b",
                         "FetchFromApi treats a 403 like any other failure")

    def test_the_cache_write_is_conditional(self):
        conditions = _enclosing_conditions(self.get, "_userCache[cacheKey] =")
        self.assertTrue(
            conditions,
            "GetHomeConfig stores the fetch result under the user's key "
            "unconditionally, so a 401/403 for one caller's token is served to "
            "the user's other requests until the entry expires")

    def test_the_condition_comes_from_the_fetch(self):
        """Whatever gates the write has to be something FetchFromApi reported."""
        conditions = " ".join(_enclosing_conditions(self.get, "_userCache[cacheKey] ="))
        call = re.search(r"FetchFromApi\(([^;]*)\)\s*;", self.get).group(0)
        names = set(re.findall(r"[A-Za-z_]\w*", conditions)) - {"if"}
        reported = {n for n in names if re.search(rf"\b{n}\b", call)}
        self.assertTrue(reported,
                        f"the cache write is gated by `{conditions}`, which the "
                        f"fetch `{call}` has no part in")

    def test_other_failures_are_still_cached_briefly(self):
        """The fetch blocks a request thread for up to its timeout; an unreachable
        backend must not cost that on every home request."""
        catch = self.fetch[self.fetch.rindex("catch"):]
        self.assertNotRegex(catch, r"[A-Za-z]*[Rr]efused\s*=\s*true",
                            "a timeout/connection failure is being treated as a "
                            "refusal, which turns off the negative cache that "
                            "protects Jellyfin's request threads during an outage")


if __name__ == "__main__":
    unittest.main()
