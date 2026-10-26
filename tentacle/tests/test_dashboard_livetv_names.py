"""Live TV and provider names show exactly as the provider sends them.

Run from the tentacle/ directory:  python -m unittest discover -s tests

Category, group and channel names, channel logo addresses and the provider's
own messages come from the IPTV provider and were put into the dashboard's
markup as they were: a name with a double quote cut its tooltip or its
option value short, one with an ampersand sequence showed the wrong text.
They are now escaped where they are inserted. The tests run the real
functions under node with such values and check the text a browser shows.
"""
import json
import unittest

from dashboard_js import HAVE_NODE, Page, functions, missing_text, render

NAMES = ["O'Brien & \"Sons\" <3", "Tom &#39; Jerry &amp; co", "Ocean’s “Eleven”",
         "Šėšta `tick` ${x} </div>", "a\\\\b\\"]
ESC = functions("pages.js", ["escapeAttr", "escapeJS"]) + functions("app.js", ["escHtml", "dashTimeAgo"])


@unittest.skipUnless(HAVE_NODE, "node is not installed")
class LiveTvNames(unittest.TestCase):
    def assertShown(self, values, written):
        html = [h for hs in written.values() for h in hs]
        self.assertEqual([], missing_text(values, html))

    def test_provider_card(self):
        src = ESC + "const srcState = {open: {}};\n" + functions(
            "sources.js", ["renderProviderCard", "_providerAddress", "_switchRow", "_providerSection", "_liveLimitsRow", "_liveNumbersRow"])
        for i, name in enumerate(NAMES):
            p = {"id": 1, "name": name, "server_url": f"http://prov.test/{i}?a=1&b=2", "status": "ok",
                 "has_vod": True, "has_live": True, "active": True, "live_tv_enabled": True}
            html = render(src, "", "written.card = [renderProviderCard(%s)]" % json.dumps(p))
            self.assertShown([name, p["server_url"]], html)

    def test_address_panel(self):
        """What a provider's address test says (and the addresses pasted
        from its message) show as written (#704)."""
        src = ESC + "const srcState = {open: {}, addr: {}};\n" + functions(
            "sources.js", ["renderProvAddrPanel", "_addrWord", "_addrDetails", "_addrDay", "_addrHost"])
        rows = [{"id": i, "url": f"http://h{i}.test", "in_use": i == 0, "preferred": False,
                 "tested_at": "2026-10-20T10:00:00Z", "ok": False, "word": n, "live_word": n,
                 "vod_word": None, "streams_skipped": None, "day_good": 1, "day_bad": 2}
                for i, n in enumerate(NAMES)]
        data = {"addresses": rows, "test": {"running": False}}
        html = render(src, "", "srcState.addr[1] = %s; renderProvAddrPanel(1)" % json.dumps(data))
        self.assertShown([n[:1].upper() + n[1:] for n in NAMES], html)

    def test_category_list(self):
        src = ESC + "const state = {catFilter: 'all', categories: []};\n" + functions("app.js", ["renderCatList"])
        cats = [{"id": i, "name": n, "type": "movie", "source_tag": n + " tag", "whitelisted": True}
                for i, n in enumerate(NAMES)]
        html = render(src, "state.categories = %s;" % json.dumps(cats), "renderCatList()")
        self.assertShown(NAMES + [n + " tag" for n in NAMES], html)
        titles = Page(html["cat-list"][-1]).attrs("title")
        self.assertEqual(NAMES, titles)

    def test_groups_filter_and_channels(self):
        src = ESC + "const liveState = {dirtyChannels: {}};\n" + functions(
            "pages.js", ["populateGroupFilter", "renderLiveChannels", "_liveGuideTitle"]) + \
            "const srcState = {groups: {}};\n" + functions("sources.js", ["renderProvGroups", "_provGroupsShown"])
        groups = [{"id": i, "name": n, "enabled": True, "channel_count": 3} for i, n in enumerate(NAMES)]
        chans = [{"id": i, "name": n, "group_title": n, "logo_url": f"http://logo.test/{i}.png?s=1&t=2",
                  "enabled": True, "has_epg_data": False} for i, n in enumerate(NAMES)]
        html = render(src, "", "srcState.groups[7] = %s; renderProvGroups(7); populateGroupFilter(%s); renderLiveChannels(%s, 5)"
                      % (json.dumps(groups), json.dumps(groups), json.dumps(chans)))
        self.assertShown(NAMES, {"g": html["prov-groups-list-7"]})
        options = Page(html["live-ch-group-filter"][-1]).attrs("value")
        self.assertEqual(sorted(NAMES, key=lambda s: s), sorted(options[1:]))
        labels = Page(html["prov-groups-list-7"][-1]).attrs("aria-label")
        self.assertEqual([n + " in Live TV" for n in NAMES], labels)
        logos = Page(html["live-channels"][-1]).attrs("src")
        self.assertEqual([c["logo_url"] for c in chans], logos)

    def test_migrate_provider_names(self):
        src = ESC + "const state = {providers: null}; function showModal() {}\n" + functions("pages.js", ["showMigrate"])
        provs = [{"id": i, "name": n} for i, n in enumerate(NAMES)]
        html = render(src, "API['/api/providers'] = %s;" % json.dumps(provs), "await showMigrate()")
        self.assertEqual(NAMES, [t for t in Page(html["migrate-from"][-1]).texts if t.strip()])


if __name__ == "__main__":
    unittest.main()
