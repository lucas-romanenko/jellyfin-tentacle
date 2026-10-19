"""The red provider banner: one line per failing provider with the plain
reason, escaped, gone when every provider works, and Hide keeps it away
until the failure changes.

Run from the tentacle/ directory:  python -m unittest discover -s tests
"""
import json
import unittest

from dashboard_js import HAVE_NODE, Page, functions, missing_text, render

SRC = functions("app.js", ["loadProviderProblems", "hideProviderProblems"]) + "\nlet _providerProblemsHidden = '';\n"
NAME = "O'Brien & \"Sons\" <b>TV</b>"
REASON = "Provider refusing: 403 since 07:50. Likely IP block, expired account or moved URL"


def providers(*entries):
    return json.dumps({"providers": list(entries)})


FAILING = {"id": 1, "name": NAME, "state": "failing", "step": "api", "kind": "http", "code": 403,
           "since": "2026-10-08T07:50:00+00:00", "reason": REASON}
OK = {"id": 2, "name": "Fine IPTV", "state": "ok", "reason": None}


@unittest.skipUnless(HAVE_NODE, "node is not installed")
class ProviderBanner(unittest.TestCase):
    def test_failing_provider_shows_its_name_and_reason_escaped(self):
        html = render(SRC, "API['/api/health/providers'] = %s;" % providers(FAILING, OK),
                      "await loadProviderProblems()")["provider-problems-banner"]
        self.assertEqual([], missing_text([f"⚠ {NAME}: {REASON}"], html))
        page = Page(html[-1])
        self.assertNotIn("b", [tag for tag, _ in page.elements], "the provider name is text, not markup")
        self.assertNotIn("Fine IPTV", page.text)
        handlers = page.attrs("onclick")
        self.assertIn("checkProvidersNow()", handlers)
        self.assertIn("hideProviderProblems()", handlers)

    def test_all_ok_hides_the_banner(self):
        out = render(SRC, "API['/api/health/providers'] = %s;" % providers(OK),
                     "await loadProviderProblems(); written.display = [document.getElementById("
                     "'provider-problems-banner').style.display]")
        self.assertEqual(out["display"], ["none"])
        self.assertNotIn("provider-problems-banner", out)

    def test_hide_holds_until_the_failure_changes(self):
        changed = dict(FAILING, code=401, kind="login")
        out = render(SRC, "API['/api/health/providers'] = %s;" % providers(FAILING),
                     "const el = document.getElementById('provider-problems-banner');"
                     "await loadProviderProblems(); hideProviderProblems();"
                     "await loadProviderProblems(); written.after_hide = [el.style.display];"
                     "API['/api/health/providers'] = %s;"
                     "await loadProviderProblems(); written.after_change = [el.style.display];"
                     % providers(changed))
        self.assertEqual(out["after_hide"], ["none"])
        self.assertEqual(out["after_change"], [""])


if __name__ == "__main__":
    unittest.main()
