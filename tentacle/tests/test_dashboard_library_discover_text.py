"""Titles, overviews, genres and poster addresses show exactly as stored.

Run from the tentacle/ directory:  python -m unittest discover -s tests

Library, Discover, coverage and duplicate cards, the Add / Manage episodes /
Download more dialogs and the detail views put titles, TMDB overviews and
genres, tags, episode and season names, paths, error text and poster
addresses into the page as they came. A title with an ampersand sequence
showed different text, and a poster address with a double quote cut its
image short. They are now escaped where inserted; values Lucas's code
already escaped are left as they were (the tests check the exact text, so an
escape applied twice would show).
"""
import json
import unittest

from dashboard_js import HAVE_NODE, Page, functions, missing, missing_text, render

VALUES = ["O'Brien & \"Sons\" <3", "Tom &#39; Jerry &amp; co", "Ocean’s “Eleven”",
          "Šėšta `tick` ${x} </div>", "a\\\\b\\"]
ESC = functions("pages.js", ["escapeAttr", "escapeJS", "_imgUrl", "_trailerBtn"]) + functions("app.js", ["escHtml"])


@unittest.skipUnless(HAVE_NODE, "node is not installed")
class LibraryAndDiscoverText(unittest.TestCase):
    def shown(self, values, html_list):
        self.assertEqual([], missing(values, html_list))

    def shown_as_text(self, values, html_list):
        self.assertEqual([], missing_text(values, html_list))

    def test_library_card(self):
        src = ESC + functions("pages.js", ["renderLibCard"])
        for i, v in enumerate(VALUES):
            item = {"tmdb_id": i + 1, "media_type": "movie", "title": v, "year": "19\"99",
                    "source_tag": v + " tag", "poster_path": f"/p{i}\"x.jpg", "in_library": True}
            html = render(src, "", "written.x = [renderLibCard(%s)]" % json.dumps(item))["x"]
            self.shown_as_text([v, v + " tag", '19"99'], html)
            self.assertEqual([f"https://image.tmdb.org/t/p/w185/p{i}\"x.jpg"], Page(html[0]).attrs("src"))

    def test_discover_grid_and_coverage_card(self):
        src = ESC + ("function _discoverDownloadInfo() { return null; } function _discoverUnreleasedInfo() { return null; }\n"
                     + functions("pages.js", ["renderDiscoverGrid", "coverageCard"]))
        items = [{"tmdb_id": i + 1, "media_type": "movie", "title": v, "year": "2001", "rating": 7.5,
                  "poster_path": f"http://img.test/{i}.jpg?a=1&b=\"2\"", "list_name": v + " list"}
                 for i, v in enumerate(VALUES)]
        html = render(src, "", "renderDiscoverGrid(%s); written.cov = %s.map(i => coverageCard(i, true))"
                      % (json.dumps(items), json.dumps(items)))
        grid = html["discover-grid"]
        self.shown_as_text(VALUES + [v + " list" for v in VALUES], grid)
        self.assertEqual([i["poster_path"] for i in items], Page(grid[-1]).attrs("src"))
        self.shown_as_text([f"{v} (2001)" for v in VALUES], html["cov"])
        self.assertEqual([i["poster_path"] for i in items], [a for h in html["cov"] for a in Page(h).attrs("src")])

    def test_media_detail(self):
        src = ESC + ("let _detailSeq = 0; function _detailOpening() { return _detailSeq; }\n"
                     "function showModal() {} function closeModal() {} const state = {currentUser: {is_admin: true}};\n"
                     + functions("pages.js", ["showMediaDetail"]))
        for v in VALUES:
            data = {"title": v, "year": 2001, "rating": 7, "overview": v + " overview", "genres": [v + " genre"],
                    "tags": [v + " tag"], "source": "provider_x", "strm_path": "/media/vod/" + v + ".strm",
                    "poster_path": "/p.jpg", "is_vod": False}
            html = render(src, "API['/api/library/item/'] = %s; API['/api/library/tmdb/'] = {};" % json.dumps(data),
                          "await showMediaDetail(7, 'movie')")
            body = html["detail-body"]
            self.shown_as_text([v + " overview", v + " genre", v + " tag", "/media/vod/" + v + ".strm"], body)

    def test_episode_picker_names(self):
        src = ESC + ("const _vodEpisodes = {}, _dlEpisodes = {}; function _updateSeasonStatus() {}\n"
                     + functions("pages.js", ["_renderEpisodes"]))
        eps = [{"episode_number": i + 1, "name": v, "air_date": "2020-01-0" + str(i + 1)} for i, v in enumerate(VALUES)]
        html = render(src, "const _epPickerLoaded = {1: %s};" % json.dumps(eps), "_renderEpisodes(1)")
        self.shown_as_text(VALUES, html["ep-list-1"])

    def test_activity_poster_address_and_bad_poster_cache(self):
        src = ESC + "const _activityBadPosters = new Set();\n" + functions("pages.js", ["_activityPoster"])
        addr = 'http://img.test/p"a\'th&x=1.jpg'
        html = render(src, "", "written.a = [_activityPoster(%s)]; _activityBadPosters.add(%s); written.b = [_activityPoster(%s)]"
                      % (json.dumps(addr), json.dumps(addr), json.dumps(addr)))
        self.assertEqual([addr], Page(html["a"][0]).attrs("src"))  # getAttribute('src') gives this back
        self.assertEqual([], Page(html["b"][0]).attrs("src"))       # a known bad poster shows the placeholder


if __name__ == "__main__":
    unittest.main()
