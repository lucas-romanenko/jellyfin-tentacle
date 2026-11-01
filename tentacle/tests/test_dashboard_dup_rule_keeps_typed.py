"""Health → Duplicates: what the admin types into the automatic rule stays
until Save, and Save sends it (#899).

Run from the tentacle/ directory:  python -m unittest discover -s tests

The qBittorrent login and protected indexers sit in the "Downloads that stay"
fold-out, but the only Save was in the top row, and anything that reloaded the
section (resolving a duplicate, Run now, a list change, another mode) rebuilt
the form from the saved rule, so typed values vanished and no PUT was sent.
These run the real section code under node: type, reload, check the markup
still carries the typed values, then Save and check the PUT body.
"""
import json
import re
import unittest

from dashboard_js import HAVE_NODE, Page, functions, render

SRC = functions("pages.js", ["escapeAttr", "escapeJS", "loadDupRule", "_dupRuleCounts", "dupRuleEdit",
                             "_dupRuleUnsavedHint", "renderDupRule", "setDupRuleMode", "saveDupRule",
                             "_renderDupRulePlan"])
_JS = open(__file__.rsplit("/", 2)[0] + "/static/js/pages.js", encoding="utf-8").read()
for name in ("_DUP_RULE_SAY", "_DUP_RULE_FIELDS"):
    m = re.search(r"^const %s = \{.*?^\};\n" % name, _JS, re.M | re.S)
    SRC += "\n" + m.group(0)

RULE = {"mode": "stream", "cap": 25, "every_minutes": 30, "last": None, "recycle_bins": {},
        "protected_indexers": "", "qbittorrent_url": "", "qbittorrent_username": "",
        "qbittorrent_password": "", "qbittorrent_tag": ""}

SETUP = r"""
const pages = { dup: { filter: 'pending', items: [] } };
API['/api/duplicates/rule'] = %s;
API['/api/duplicates/prefer-download'] = [];
API['/api/library/strm-managed'] = [];
const puts = [];
const _fakeApi = api;
api = async (path, opts) => {
  if (opts && opts.method === 'PUT') { puts.push(opts.body);
    const saved = Object.assign({}, API[path], opts.body, { qbittorrent_password: opts.body.qbittorrent_password ? '********' : '' });
    API[path] = saved; return JSON.parse(JSON.stringify(saved)); }
  return _fakeApi(path);
};
const type = (id, value) => dupRuleEdit({ id, value });
""" % json.dumps(RULE)


def run(call):
    out = render(SRC, SETUP, call + "; written.__puts = [JSON.stringify(puts)]")
    return out, json.loads(out.pop("__puts")[0])


def inputs(html):
    return {a["id"]: a.get("value", "") for t, a in Page(html).elements if t == "input" and "id" in a}


@unittest.skipUnless(HAVE_NODE, "node is not installed")
class DupRuleKeepsTyped(unittest.TestCase):
    TYPED = "type('dup-rule-qb-url', 'http://qb:8080'); type('dup-rule-qb-user', 'admin');" \
            "type('dup-rule-qb-pass', 's3cret'); type('dup-rule-indexers', 'PrivateHD')"

    def test_a_reload_keeps_what_was_typed(self):
        # loadDupRule runs after a resolve, Run now and list changes.
        out, _ = run("await loadDupRule(); %s; await loadDupRule()" % self.TYPED)
        last = inputs(out["dup-rule"][-1])
        self.assertEqual(last["dup-rule-qb-url"], "http://qb:8080")
        self.assertEqual(last["dup-rule-qb-user"], "admin")
        self.assertEqual(last["dup-rule-qb-pass"], "s3cret")
        self.assertEqual(last["dup-rule-indexers"], "PrivateHD")
        self.assertIn("Not saved yet", out["dup-rule"][-1])

    def test_another_mode_keeps_what_was_typed(self):
        out, _ = run("await loadDupRule(); type('dup-rule-cap', '7'); %s; setDupRuleMode('download');"
                     "setDupRuleMode('stream')" % self.TYPED)
        last = inputs(out["dup-rule"][-1])
        self.assertEqual(last["dup-rule-cap"], "7")
        self.assertEqual(last["dup-rule-qb-url"], "http://qb:8080")

    def test_the_fold_out_has_its_own_save(self):
        out, _ = run("await loadDupRule()")
        html = out["dup-rule"][-1]
        fold = html[html.index('<details class="dup-rule-more"'):html.index("</details>")]
        self.assertIn('onclick="saveDupRule(this)"', fold)

    def test_save_sends_the_typed_values_and_a_reload_shows_them(self):
        out, puts = run("await loadDupRule(); %s; await loadDupRule(); await saveDupRule(); await loadDupRule()"
                        % self.TYPED)
        self.assertEqual(len(puts), 1)
        self.assertEqual(puts[0]["qbittorrent_url"], "http://qb:8080")
        self.assertEqual(puts[0]["qbittorrent_username"], "admin")
        self.assertEqual(puts[0]["qbittorrent_password"], "s3cret")
        self.assertEqual(puts[0]["protected_indexers"], "PrivateHD")
        last = out["dup-rule"][-1]
        self.assertEqual(inputs(last)["dup-rule-qb-url"], "http://qb:8080")
        self.assertEqual(inputs(last)["dup-rule-qb-pass"], "********")
        self.assertIn("Password saved", last)
        self.assertNotIn("Not saved yet", last)

    def test_password_hint_says_when_none_is_saved(self):
        out, _ = run("await loadDupRule()")
        self.assertIn("No password saved", out["dup-rule"][-1])


if __name__ == "__main__":
    unittest.main()
