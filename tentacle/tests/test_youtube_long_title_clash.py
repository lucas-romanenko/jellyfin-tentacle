"""A long multi-byte YouTube title that clashes must not hang add_channel (round 2).

e37ea6f kept the " (n)" suffix inside 120 CHARACTERS, but safe_name() also
cuts a folder name at 255 BYTES. 116 CJK characters are 348 bytes, so the byte
cut dropped the suffix, the folder name never changed, and the numbering loop
ran for ever at 100% CPU — "日本語のプレイリスト"*15 added twice, or "🎵"*100 by
two owners. Each test runs under a hard timeout.

Run from tentacle/:  python -m unittest discover -s tests -p "test_youtube_long_title_clash.py"
"""
import signal
import unittest

import test_youtube


class _Timeout(Exception):
    pass


def _alarm(signum, frame):
    raise _Timeout("add_channel did not return")


class TestLongMultiByteTitles(unittest.TestCase):
    setUp = test_youtube.TestAddingAChannel.setUp
    tearDown = test_youtube.TestAddingAChannel.tearDown
    _Req = test_youtube.TestAddingAChannel._Req
    _add = test_youtube.TestAddingAChannel._add

    def _pl(self, pid, title, owner):
        self.info.update(kind="playlist", playlist_id=pid, title=title, owner=owner,
                         channel_id="UC" + "j" * 22,
                         canonical=f"https://www.youtube.com/playlist?list={pid}")

    def _add_within(self, seconds=10):
        old = signal.signal(signal.SIGALRM, _alarm)
        signal.alarm(seconds)
        try:
            return self._add()
        finally:
            signal.alarm(0)
            signal.signal(signal.SIGALRM, old)

    def _folders(self):
        from services.youtube import library
        return [library.safe_name(r.title) for r in self.db.query(self.YouTubeChannel)]

    def test_cjk_title_added_twice(self):
        t = "日本語のプレイリスト" * 15               # 150 characters, 450 bytes
        self._pl("PL1", t, "Ann")
        self._add_within()
        self._pl("PL2", t, "Ann")                   # same owner: no "(owner)" step
        self._add_within()
        folders = self._folders()
        self.assertEqual(len(set(folders)), 2)
        self.assertTrue(all(len(f.encode()) <= 255 for f in folders))

    def test_emoji_title_by_two_owners_and_a_third_copy(self):
        t = "🎵" * 100
        for pid, owner in (("PL1", "Ann"), ("PL2", "Bob"), ("PL3", "Bob")):
            self._pl(pid, t, owner)
            self._add_within()
        self.assertEqual(len(set(self._folders())), 3)

    def test_numbering_is_bounded(self):
        from fastapi import HTTPException
        from routers import youtube
        self._pl("PL1", "Mix", "Mix")
        self._add_within()
        old = youtube._MAX_TITLE_NUMBER
        youtube._MAX_TITLE_NUMBER = 3
        try:
            for pid in ("PL2", "PL3"):
                self._pl(pid, "Mix", "Mix")
                self._add_within()
            self._pl("PL4", "Mix", "Mix")
            with self.assertRaises(HTTPException) as cm:
                self._add_within()
            self.assertEqual(cm.exception.status_code, 409)
        finally:
            youtube._MAX_TITLE_NUMBER = old


if __name__ == "__main__":
    unittest.main()
