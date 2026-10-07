"""A provider's language tag is not a country: "EN: Discovery Channel" takes
its guide by name like "Discovery Channel" does (#141).

Run from the tentacle/ directory:  python -m unittest tests.test_epg_language_tag_not_country

The name pass only takes a feed channel "of the channel's own country (or
one that names no country)" (services/epg_match.py). The channel's country
comes from channel_country() (services/channel_names.py), which read ANY
two-letter leading tag as a country code. Many panels tag English channels
"EN:" / "|EN|" (the language), and "en" is no country's code: a feed
channel that names a country ("DiscoveryChannel.us") was "another
country's" to it. "EN: Discovery Channel" was never matched against
DiscoveryChannel.us and was reported as "a name the feed has only for
another country", while the very same name without the tag was matched.

Real country tags must keep working exactly as before: "UK:", "CA EN:",
"CA FR:", "US:", "CA:", "LT:" (Lithuanian is "lt" too, and so is the
country) are the tags of a live install's 840 channels.
"""
import itertools
import logging
import re
import string
import unicodedata
import unittest

import services.channel_names as channel_names
from services.channel_names import channel_country, feed_countries
from services.epg_match import coverage_report, coverage_summary, resolve_guide_ids


def setUpModule():
    logging.disable(logging.CRITICAL)


def tearDownModule():
    logging.disable(logging.NOTSET)


def _ch(cid, name, tvg=None):
    return {"id": cid, "name": name, "tvg_id": tvg, "override": None, "enabled": True}


FEED = [{"id": "DiscoveryChannel.us", "names": ["Discovery Channel"]}]


class LanguageTagIsNotACountry(unittest.TestCase):
    def test_the_untagged_name_matches(self):
        """Control (passes on main): the name alone finds the guide."""
        r = resolve_guide_ids([_ch(1, "Discovery Channel")], FEED)
        self.assertEqual(("name", "DiscoveryChannel.us"), (r[1]["method"], r[1]["guide_id"]))

    def test_a_country_tag_still_scopes_the_match(self):
        """Control (passes on main, must keep passing): a real country tag
        that the feed does not have stays "foreign"; "CA EN:" is Canada."""
        r = resolve_guide_ids([_ch(1, "UK: Discovery Channel"), _ch(2, "CA EN: Discovery Channel")], FEED)
        self.assertEqual(("foreign", "foreign"), (r[1]["reason"], r[2]["reason"]))

    def test_an_en_language_tag_matches_like_no_tag(self):
        for name in ("EN: Discovery Channel", "|EN| Discovery Channel", "[EN] DISCOVERY CHANNEL HD"):
            with self.subTest(name=name):
                r = resolve_guide_ids([_ch(1, name)], FEED)[1]
                self.assertNotEqual("foreign", r["reason"],
                                    f"{name!r}: the language tag 'EN' was read as a country")
                self.assertEqual(("name", "DiscoveryChannel.us"), (r["method"], r["guide_id"]))

    def test_the_sync_report_does_not_call_it_another_country(self):
        chans = [_ch(1, "EN: Discovery Channel")]
        r = resolve_guide_ids(chans, FEED)
        report = coverage_report(chans, r, {"DiscoveryChannel.us"})
        self.assertEqual(0, report["enabled"]["foreign"])
        self.assertEqual(1, report["enabled"]["with_guide"])
        self.assertNotIn("another country", coverage_summary(report))


    def test_a_language_tag_with_a_country_after_it_is_that_country(self):
        r = resolve_guide_ids([_ch(1, "EN CA: Discovery Channel"), _ch(2, "EN US: Discovery Channel")], FEED)
        self.assertEqual("foreign", r[1]["reason"])
        self.assertEqual(("name", "DiscoveryChannel.us"), (r[2]["method"], r[2]["guide_id"]))

    def test_a_feed_channel_with_a_language_tag_stays_apart(self):
        """The feed side is unchanged: a feed channel named "JA: Discovery
        Channel" is still not a US channel's guide."""
        feed = [{"id": "DiscoveryJP", "names": ["JA: Discovery Channel"]}]
        r = resolve_guide_ids([_ch(1, "US: Discovery Channel")], feed)
        self.assertEqual("foreign", r[1]["reason"])


# What channel_country() and feed_countries() returned before.
_OLD_RE = re.compile(r"^\s*[|\[(]?\s*([A-Za-z]{2}|USA)(?:[ /-][A-Za-z]{2})?\s*[|\]):]")


def _old_country(name):
    m = _OLD_RE.match(unicodedata.normalize("NFKC", name or ""))
    if not m:
        return None
    code = m.group(1).lower()
    return {"uk": "gb", "usa": "us"}.get(code, code)


def _old_feed_countries(feed_id, names):
    out = set()
    m = re.search(r"\.([A-Za-z]{2})$", feed_id or "")
    if m:
        code = m.group(1).lower()
        out.add({"uk": "gb", "usa": "us"}.get(code, code))
    out |= {c for c in (_old_country(n) for n in names or []) if c}
    return out


class RealCountryTagsAreUnchanged(unittest.TestCase):
    def test_the_tags_of_a_live_install(self):
        for name, want in (("UK: SKY ONE", "gb"), ("CA EN: DISCOVERY HD", "ca"), ("CA FR: TVA", "ca"),
                           ("US: FOX", "us"), ("CA: TSN 5 ᴿᴬᵂ", "ca"), ("LT: BTV HD", "lt"),
                           ("|UK| SKY ONE", "gb"), ("[US] FOX", "us"), ("USA: FOX", "us"),
                           ("FR: TF1", "fr"), ("DE: ARD", "de"), ("AR: MBC", "ar"), ("EU: EUROSPORT", "eu"),
                           ("CNN: International", None), ("Discovery Channel", None)):
            with self.subTest(name=name):
                self.assertEqual(want, channel_country(name))
                self.assertEqual(_old_country(name), channel_country(name))

    def test_lithuanian_channels_still_match_only_lithuanian_guides(self):
        feed = [{"id": "BTV.lt", "names": ["BTV"]}, {"id": "LRT.lt", "names": ["LRT"]}]
        r = resolve_guide_ids([_ch(1, "LT: BTV HD"), _ch(2, "LT: LRT")], feed)
        self.assertEqual([("name", "BTV.lt"), ("name", "LRT.lt")],
                         [(r[c]["method"], r[c]["guide_id"]) for c in (1, 2)])
        r = resolve_guide_ids([_ch(1, "LT: BTV HD")], [{"id": "BTV.de", "names": ["BTV"]}])
        self.assertEqual("foreign", r[1]["reason"])

    def test_every_two_letter_tag(self):
        """Every tag reads as before, except a language-only code, which now
        names no country (or the country after it). The feed side is unchanged."""
        for a in ("".join(p) for p in itertools.product(string.ascii_uppercase, repeat=2)):
            lang = a.lower() in channel_names._LANGUAGE_ONLY
            for name, if_lang in ((f"{a}: X", None), (f"|{a}| X", None), (f"[{a}] X", None), (f"({a}) X", None),
                                  (f"{a} CA: X", "ca"), (f"{a}/US| X", "us"), (f"CA {a}: X", "ca")):
                want = if_lang if lang and not name.startswith("CA ") else _old_country(name)
                self.assertEqual(want, channel_country(name), name)
                self.assertEqual(_old_feed_countries(f"x.{a.lower()}", [name]),
                                 feed_countries(f"x.{a.lower()}", [name]), name)

    def test_uk_and_eu_stay_regions(self):
        self.assertNotIn("uk", channel_names._LANGUAGE_ONLY)
        self.assertNotIn("eu", channel_names._LANGUAGE_ONLY)
        for code in ("fr", "de", "es", "lt", "ar", "it", "pt", "nl", "sv"):
            self.assertNotIn(code, channel_names._LANGUAGE_ONLY)


if __name__ == "__main__":
    unittest.main()
