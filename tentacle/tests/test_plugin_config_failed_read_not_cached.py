"""/Tentacle/Config must not cache a failed read of the backend's keys (#258).

Run from the tentacle/ directory:  python -m unittest discover -s tests

`TentacleConfigController.GetConfig` tells the web client whether TMDB and
MDBList ratings are on, and caches the answer for everyone for 5 minutes.
When its read of /api/settings/plugin-keys failed (backend restarting, a 5xx,
a caller whose token the backend refused), it cached "both off" all the same:
every user got ratings switched off for up to 5 minutes, and tabs that loaded
then kept "off" for the session (the client only retries a non-OK answer).

Now only a successful read is cached. A refusal (401/403) goes back to that
caller only; any other failure serves the last good answer, or 503 when
there is none, which the client retries.

Source-level, like the other plugin tests: the repository has no C# test host.
"""
import re
import unittest
from pathlib import Path

CONTROLLER = Path(__file__).resolve().parents[2] / "tentacle-plugin" / "Api" / "ConfigController.cs"
MDBLIST_JS = Path(__file__).resolve().parents[2] / "tentacle-plugin" / "Inject" / "tentacle-mdblist.js"


def _code() -> str:
    return re.sub(r"//[^\n]*", "", CONTROLLER.read_text(encoding="utf-8"))


def _enclosing_headers(body: str, needle: str):
    pos = body.index(needle)
    stack = []
    for m in re.finditer(r"[{}]", body[:pos]):
        if m.group() == "{":
            stack.append(body[:m.start()].rstrip().splitlines()[-1].strip())
        else:
            stack.pop()
    return stack


class FailedReadIsNotCached(unittest.TestCase):
    def setUp(self):
        src = _code()
        self.get = src[src.index("public async Task<ActionResult> GetConfig("):src.index("private static string BuildConfigJson(")]

    def test_the_shared_cache_is_written_only_after_a_successful_read(self):
        # The write sits inside the try that reads the backend, after the read.
        headers = _enclosing_headers(self.get, "_cachedConfig = configJson")
        write = self.get.index("_cachedConfig = configJson")
        read = self.get.index("PluginKeysClient.GetSecuredStringAsync(")
        first_catch = self.get.index("catch", read)
        self.assertTrue(read < write < first_catch and "try" in headers,
                        "the config is cached whether or not the backend answered")

    def test_a_refused_caller_gets_its_own_refusal(self):
        self.assertRegex(self.get, r"HttpStatusCode\.Unauthorized")
        self.assertRegex(self.get, r"HttpStatusCode\.Forbidden")
        self.assertRegex(self.get, r"return StatusCode\(\s*\(int\)")

    def test_another_failure_serves_the_last_good_answer_or_503(self):
        self.assertIn("_lastGoodConfig = configJson", self.get)
        catch = self.get[self.get.rindex("catch (Exception"):]
        self.assertIn("return LastGoodOrUnavailable();", catch)
        src = _code()
        helper = src[src.index("private ActionResult LastGoodOrUnavailable()"):]
        helper = helper[:helper.index("private static string BuildConfigJson(")]
        self.assertIn("_lastGoodConfig", helper)
        self.assertRegex(helper, r"StatusCode\(\s*503")

    def test_a_failing_backend_is_not_asked_by_every_request(self):
        """No negative cache for everyone, but a short pause before asking again:
        each request would otherwise wait out the 10 s timeout behind the lock."""
        catch = self.get[self.get.rindex("catch (Exception"):]
        self.assertRegex(catch, r"_retryAfter\s*=\s*DateTime\.UtcNow\.Add\(")
        self.assertRegex(self.get, r"if \(DateTime\.UtcNow < _retryAfter\)\s*\{\s*return LastGoodOrUnavailable\(\);")

    def test_the_client_retries_a_non_ok_answer(self):
        js = MDBLIST_JS.read_text(encoding="utf-8")
        load = js[js.index("function loadTentacleConfig"):]
        self.assertIn("if (!r.ok) throw", load)
        self.assertRegex(load, r"setTimeout\(loadTentacleConfig, \d+\)")


if __name__ == "__main__":
    unittest.main()
