"""Jellyfin Web's Tentacle Recordings page shows when each recording was made.

Lucas, 2026-10-24: "Date underneath still shows jan 1 12:00am". Finished
recordings from LiveTv/Recordings have no StartDate, and the page asked
only for Overview, so DateCreated was missing too and Date.parse(0) put
1 Jan 2000 under every card. The page now asks for DateCreated and
PremiereDate, dates and sorts by them, and shows no time when there is
no date.

Runs the real renderRecordings under node with stubs (skipped without
node); the Fields check needs no node.

Run from the tentacle/ directory:  python -m unittest tests.test_recordings_web_dates
"""
import re
import unittest

from inject_js import ESC_DOM, HAVE_NODE, INJECT, Page, node

SRC = (INJECT / "tentacle-recordings.js").read_text(encoding="utf-8")


def _function(name):
    start = SRC.index("  function " + name + "(")
    return SRC[start:SRC.index("\n  }\n", start) + 4]


class AsksForTheDates(unittest.TestCase):
    def test_the_recordings_request_asks_for_date_created(self):
        fields = re.search(r"apiGet\('LiveTv/Recordings', \{[^}]*Fields: '([^']*)'", SRC).group(1).split(",")
        self.assertIn("DateCreated", fields)
        self.assertIn("PremiereDate", fields)

    def test_no_date_never_falls_back_to_zero(self):
        self.assertNotRegex(SRC, r"Date\.parse\([^)]*\|\| 0\)")


@unittest.skipUnless(HAVE_NODE, "node is not installed")
class CardDates(unittest.TestCase):
    def render(self, items):
        out = node(ESC_DOM + r"""
          var window = { ApiClient: { getUrl: function (p) { return '/' + p; } } };
        """ + "".join(_function(f) for f in ("esc", "api", "fmtTime", "dayOf", "renderRecordings")) + r"""
          console.log(JSON.stringify(renderRecordings({ Items: %s })));
        """ % items)
        page = Page(out)
        return [t for t in page.texts if t.strip()]

    def test_a_finished_recording_shows_when_it_recorded_not_jan_1(self):
        texts = self.render("""[
          { Id: 'a', Name: 'Live: NHL Hockey', Status: 'Completed', StartDate: null,
            DateCreated: '2026-10-23T23:55:00.0000000Z', PremiereDate: '2026-10-23T23:55:00.0000000Z', ChannelName: 'TSN 2' },
          { Id: 'b', Name: 'Canadiens at Blackhawks', Status: 'Completed',
            DateCreated: '2026-10-24T02:58:00.0000000Z', ChannelName: 'TSN 2' }]""")
        metas = [t for t in texts if 'TSN 2' in t]
        self.assertEqual(2, len(metas))
        self.assertFalse(any('Jan 1' in m or 'Jan 01' in m for m in metas), metas)
        # newest first
        self.assertEqual(['Recorded', 'Canadiens at Blackhawks', metas[0], 'Live: NHL Hockey', metas[1]], texts)

    def test_premiere_date_when_jellyfin_gives_no_date_created(self):
        texts = self.render("""[{ Id: 'a', Name: 'News', Status: 'Completed',
          PremiereDate: '2026-10-23T23:55:00Z', ChannelName: 'CP24' }]""")
        self.assertNotEqual('CP24', texts[-1].strip(' ·'))
        self.assertNotIn('Jan 1', texts[-1])

    def test_no_date_shows_no_time(self):
        texts = self.render("[{ Id: 'a', Name: 'News', Status: 'Completed', ChannelName: 'CP24' }]")
        self.assertEqual(' · CP24', texts[-1])


if __name__ == "__main__":
    unittest.main()
