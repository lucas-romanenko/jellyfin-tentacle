"""The red provider banner: one line per failing provider with the plain
reason, escaped, gone when every provider works, and Hide keeps it away
until the failure changes. When the address in use fails and another
works, it names both with Switch / Keep (nothing switches by itself).

Run from the tentacle/ directory:  python -m unittest discover -s tests
"""
import json
import unittest

from dashboard_js import HAVE_NODE, Page, functions, missing_text, render

SRC = functions("app.js", ["loadProviderProblems", "hideProviderProblems", "_providerOffer",
                           "switchProviderAddress", "keepProviderAddress", "testProviderOthers"]) \
    + "\nlet _providerProblemsHidden = '';\n"
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


    def test_switch_and_keep_name_both_addresses(self):
        offered = dict(FAILING, other_addresses=2, address_offer={
            "from": "http://cf.bearsp.xyz", "to": "http://bearsp.<b>xyz</b>", "address_id": 7,
            "word": "login works", "tested_at": None})
        html = render(SRC, "API['/api/health/providers'] = %s;" % providers(offered),
                      "await loadProviderProblems()")["provider-problems-banner"]
        self.assertEqual([], missing_text([
            "The address in use, cf.bearsp.xyz, fails its checks. bearsp.<b>xyz</b> answers and accepts "
            "the login. Nothing changes until you choose.",
            "Switch to bearsp.<b>xyz</b>", "Keep cf.bearsp.xyz"], html))
        page = Page(html[-1])
        self.assertNotIn("b", [tag for tag, _ in page.elements], "an address is text, not markup")
        handlers = page.attrs("onclick")
        self.assertIn("switchProviderAddress(1, 7, this)", handlers)
        self.assertIn("keepProviderAddress(1, 7, this)", handlers)

    def test_no_offer_yet_can_check_the_other_addresses(self):
        html = render(SRC, "API['/api/health/providers'] = %s;" % providers(dict(FAILING, other_addresses=1)),
                      "await loadProviderProblems()")["provider-problems-banner"]
        self.assertEqual([], missing_text(["It has another address; none is known to work right now."], html))
        self.assertIn("testProviderOthers(1, this)", Page(html[-1]).attrs("onclick"))
        # A provider with one address only: no such line
        html = render(SRC, "API['/api/health/providers'] = %s;" % providers(FAILING),
                      "await loadProviderProblems()")["provider-problems-banner"]
        self.assertNotIn("testProviderOthers(1, this)", Page(html[-1]).attrs("onclick"))

    def test_the_buttons_call_use_and_keep(self):
        out = render(SRC, "const calls = []; const real = api;"
                          "api = async (path, opts) => { calls.push([path, (opts || {}).method]); return real(path); };"
                          "API['/api/providers/1/addresses/7/use'] = {switched: true, to: 'http://b.test'};"
                          "API['/api/providers/1/addresses/7/keep'] = {};"
                          "API['/api/health/providers'] = %s;" % providers(OK),
                     "await switchProviderAddress(1, 7, null); await keepProviderAddress(1, 7, null);"
                     "written.calls = calls.filter(c => c[1]).map(c => c.join(' '))")
        self.assertEqual(["/api/providers/1/addresses/7/use POST", "/api/providers/1/addresses/7/keep POST"],
                         out["calls"])

    def test_a_new_offer_shows_a_hidden_banner_again(self):
        offered = dict(FAILING, address_offer={"from": "http://a.test", "to": "http://b.test", "address_id": 7})
        out = render(SRC, "API['/api/health/providers'] = %s;" % providers(FAILING),
                     "const el = document.getElementById('provider-problems-banner');"
                     "await loadProviderProblems(); hideProviderProblems();"
                     "API['/api/health/providers'] = %s;"
                     "await loadProviderProblems(); written.after_offer = [el.style.display];"
                     % providers(offered))
        self.assertEqual(out["after_offer"], [""])


if __name__ == "__main__":
    unittest.main()
