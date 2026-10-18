"""Jellyfin page: playlist, row and rule names show exactly as stored.

Run from the tentacle/ directory:  python -m unittest discover -s tests

The SmartLists table (name, playlist, media, path), the hero and add-row
pickers, the home row list, the automatic playlists (name, origin) and the
playlist rules (name, condition values, list and provider options) put
names into the page as they came; playlist and rule names are typed by
users, provider and list names come from other services. A name with an
"&...;" sequence showed different text, and angle brackets could hide part
of it. They are now escaped where inserted.
"""
import json
import re
import unittest

from dashboard_js import HAVE_NODE, JS, Page, functions, missing_text, render

VALUES = ["O'Brien & \"Sons\" <3", "Tom &#39; Jerry &amp; co", "Ocean’s “Eleven”",
          "Šėšta `tick` ${x} </div>", "a\\\\b\\"]
ESC = functions("pages.js", ["escapeAttr", "escapeJS"]) + functions("app.js", ["escHtml"])


def _const(name):
    src = (JS / "pages.js").read_text(encoding="utf-8")
    m = re.search(r"^const %s = [\s\S]*?;\n" % name, src, re.M)
    return m.group(0)


@unittest.skipUnless(HAVE_NODE, "node is not installed")
class HomeAndPlaylistNames(unittest.TestCase):
    def shown(self, values, html):
        self.assertEqual([], missing_text(values, html))

    def test_smartlists_table(self):
        src = ESC + functions("pages.js", ["loadSmartLists"])
        data = {"path": "/data/" + VALUES[0], "path_exists": True,
                "smartlists": [{"name": v, "tag": v + " tag", "media_type": [v + " media"], "exists_on_disk": True}
                               for v in VALUES]}
        html = render(src, "API['/api/smartlists'] = %s;" % json.dumps(data), "await loadSmartLists()")
        self.shown(VALUES + [v + s for v in VALUES for s in (" tag", " media")],
                   html.get("smartlists-table", []) + html.get("smartlists-path-status", []))

    def test_home_rows_and_add_row_picker(self):
        src = (ESC + "let homeRows = [];\n" + functions(
            "pages.js", ["rowKey", "renderHomeRows", "makeSortable", "showAddHomeRow"]))
        rows = [{"type": "playlist", "playlist_id": f"p{i}", "display_name": v} for i, v in enumerate(VALUES)]
        setup = ("API['/api/smartlists/available-playlists'] = %s; API['/api/smartlists/builtin-sections'] = %s;"
                 % (json.dumps({"playlists": [{"playlist_id": f"q{i}", "name": v + " pl"} for i, v in enumerate(VALUES)]}),
                    json.dumps({"sections": [{"section_id": "s", "display_name": VALUES[1] + " sec"}]})))
        html = render(src, setup, "homeRows = %s; try { renderHomeRows(); } catch (e) {} await showAddHomeRow()"
                      % json.dumps(rows))
        self.shown(VALUES, html["home-rows-list"])
        self.shown([v + " pl" for v in VALUES] + [VALUES[1] + " sec"], ["".join(html["add-row-select"])])

    def test_auto_playlists_and_rules(self):
        src = (ESC + _const("_autoCategoryLabels") + _const("_autoCategoryOrder") + _const("RULE_FIELDS")
               + _const("OP_LABELS") + "const _LOCKED_SORT_PLAYLISTS = []; const _smartlistSortCache = {};\n"
               + functions("pages.js", ["_sortDropdown", "loadAutoPlaylists", "loadTagRules"]))
        autos = {"auto_playlists": [{"key": f"k{i}", "name": v, "origin": v + " origin", "category": "source",
                                     "enabled": True, "item_count": 3} for i, v in enumerate(VALUES)]}
        rules = [{"id": i, "name": v + " rule", "apply_to": "both", "active": True, "output_tag": v,
                  "conditions": [{"field": "genre", "operator": "contains", "value": v + " val"}]}
                 for i, v in enumerate(VALUES)]
        html = render(src, "API['/api/smartlists/auto-playlists'] = %s; API['/api/tags/rules'] = %s;"
                      % (json.dumps(autos), json.dumps(rules)),
                      "await loadAutoPlaylists(); await loadTagRules()")
        self.shown(VALUES + [v + " origin" for v in VALUES], html["auto-playlists-list"])
        self.shown([v + s for v in VALUES for s in (" rule", " val")], html["tag-rules-list"])


if __name__ == "__main__":
    unittest.main()
