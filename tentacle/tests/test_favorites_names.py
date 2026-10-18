"""The Favorites tab shows item names exactly, in the card and its tooltip.

Run from the tentacle/ directory:  python -m unittest discover -s tests

tentacle-favorites.js put the item name into the card's title attribute
through esc(), which escapes & < > but not quotes, so a name with a double
quote cut the tooltip short at the quote (and the rest of the name was read
as markup). The attribute now goes through escAttr() (as tentacle-search.js
does). Runs the real script under node with a document that escapes text the
way a browser does.
"""
import json
import unittest

from inject_js import ESC_DOM, HAVE_NODE, INJECT, NAMES, Page, node

HARNESS = ESC_DOM + r"""
var container = null;
var made = 0;
var realCreate = document.createElement;
document.createElement = function (t) { var e = realCreate(t); if (made++ === 0) container = e; return e; };
var ITEMS = %s;
var ApiClient = {
  getCurrentUserId: function () { return 'u1'; },
  getUrl: function (p, q) { return p + (q ? '?' + new URLSearchParams(q) : ''); },
  getJSON: function (url) {
    if (url.indexOf('LiveTv/') === 0) return Promise.resolve({ Items: [], TotalRecordCount: 0 });
    var type = new URL('http://x/' + url).searchParams.get('IncludeItemTypes');
    var items = type === 'Movie' ? ITEMS : [];
    return Promise.resolve({ Items: items, TotalRecordCount: items.length, StartIndex: 0 });
  },
};
var window = { ApiClient: ApiClient, addEventListener: function () {}, location: { hash: '#/home?tab=1' } };
var location = window.location;
%s
setTimeout(function () { process.stdout.write(JSON.stringify(container.innerHTML)); }, 300);
"""


@unittest.skipUnless(HAVE_NODE, "node is not installed")
class FavoritesNames(unittest.TestCase):
    def test_names_show_exactly_in_card_and_tooltip(self):
        items = [{"Id": f"id{i}", "Name": n, "Type": "Movie", "ImageTags": {}} for i, n in enumerate(NAMES)]
        html = node(HARNESS % (json.dumps(items), (INJECT / "tentacle-favorites.js").read_text(encoding="utf-8")))
        page = Page(html)
        cards = [a for t, a in page.elements if "tfav-card-name" in a.get("class", "")]
        self.assertEqual(NAMES, [a["title"] for a in cards])
        for n in NAMES:
            self.assertIn(n, page.text)
        self.assertEqual(len(NAMES), len(cards), "a name added or removed an element")


if __name__ == "__main__":
    unittest.main()
