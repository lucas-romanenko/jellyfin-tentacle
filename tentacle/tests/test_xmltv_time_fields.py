"""#177: an XMLTV time that cannot be read exactly is dropped, never stored
at a wrong date.

Run from the tentacle/ directory:  python -m unittest discover -s tests

A cut field ("2026092") used to be padded into another date, an ISO form
("2026-09-25T18:00:00Z") read as 1 January plus an offset, and an offset
beyond +/-14:00 accepted. Minute precision, full precision, the offset forms
and a zone name (with or without an offset; alone it counts as UTC, as in
Jellyfin's reader) behave as before.
"""
import unittest
from datetime import datetime

from services import xmltv


class TimeFields(unittest.TestCase):
    def test_a_cut_field_is_rejected_not_guessed(self):
        t = xmltv._parse_xmltv_time
        for v in ("2026092", "202609241", "2026092418001", "202609241800001"):
            self.assertIsNone(t(v), v)

    def test_an_unknown_trailer_is_rejected_not_misread(self):
        t = xmltv._parse_xmltv_time
        for v in ("20260925180000.5 +0000", "2026-09-25T18:00:00Z", "20260925180000 +0100x",
                  "20260925180000 +1"):
            self.assertIsNone(t(v), v)

    def test_an_impossible_offset_is_rejected(self):
        t = xmltv._parse_xmltv_time
        for v in ("20260925180000 +1500", "20260925180000 -1401", "20260925180000 +0160",
                  "20260925180000 +99", "20260925180000 +05:75"):
            self.assertIsNone(t(v), v)
        self.assertEqual(datetime(2026, 9, 25, 4, 0), t("20260925180000 +1400"))
        self.assertEqual(datetime(2026, 9, 26, 6, 0), t("20260925180000 -1200"))
        self.assertEqual(datetime(2026, 9, 25, 12, 15), t("20260925180000 +0545"))

    def test_accepted_forms(self):
        t = xmltv._parse_xmltv_time
        self.assertEqual(datetime(2026, 9, 24, 16, 0), t("202609241800 +0200"))
        self.assertEqual(datetime(2026, 9, 24, 18, 0), t("2026092418"))
        self.assertEqual(datetime(2026, 1, 1, 0, 0), t("2026"))
        self.assertEqual(datetime(2026, 9, 25, 17, 0), t("20260925180000 +0100"))
        self.assertEqual(datetime(2026, 9, 25, 17, 0), t("20260925180000 +01:00"))
        self.assertEqual(datetime(2026, 9, 25, 17, 0), t("20260925180000 +01"))
        self.assertEqual(datetime(2026, 9, 25, 17, 0), t("20260925180000 +0100 BST"))
        self.assertEqual(datetime(2026, 9, 25, 18, 0), t("20260925180000 Z"))
        self.assertEqual(datetime(2026, 9, 25, 18, 0), t("20260925180000Z"))
        self.assertEqual(datetime(2026, 9, 25, 23, 0), t("20260925180000 -0500"))
        self.assertEqual(datetime(2026, 3, 24, 6, 0), t("20260324060000 BST"))


if __name__ == "__main__":
    unittest.main()
