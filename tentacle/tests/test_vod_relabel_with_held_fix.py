"""The relabel rules and the held "fixed stream" placements share the list of
streams with no TMDB match: both add (stream, source tag) and the sync must
complete. Runs when tests/test_vod_fix_follows_relisted_stream.py is present
(the fix-follows-relisted-stream change); skipped otherwise.

Run from the tentacle/ directory:  python -m unittest discover -s tests
"""
import unittest

import services.sync as sync
from test_vod_namesakes import TMDB, stream

try:
    from test_vod_fix_follows_relisted_stream import _Fixed, LABEL
except ImportError:   # that change is not in this tree
    _Fixed, LABEL = None, None


@unittest.skipIf(_Fixed is None, "needs tests/test_vod_fix_follows_relisted_stream.py")
class HeldFixAndRelabel(_Fixed or unittest.TestCase):
    def test_label_twin_with_no_tmdb_match(self):
        self.listing(stream(LABEL, 188327)); self.night(); self.fix(); self.night()
        TMDB.search.pop("The Decline of Western Civilization", None)
        self.listing(stream(LABEL, 188327), stream(LABEL, 424242))   # old id still listed + a twin label
        run = sync.sync_provider(self.p, "full", self.db)
        self.assertEqual("completed", run.status, run.error_message)


if __name__ == "__main__":
    unittest.main()
