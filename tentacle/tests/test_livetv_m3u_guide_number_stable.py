"""An M3U channel whose URL changes keeps its guide number (#259).

Run from the tentacle/ directory:  python -m unittest discover -s tests

An M3U channel's stream_id is sha256(name|url)[:16], and the lineup and the
XMLTV publish stream_id as the GuideNumber. Jellyfin names a tuner channel
hdhr_<GuideNumber>: the channel item, its timers, series timers and every
user's favourite hang on it. #69 kept the row when a channel's URL changed
(rotated token, new password, new host) but gave it the new hash as its
stream_id, so every such sync renumbered the channels, Jellyfin deleted and
re-created them, and their timers recorded nothing. The URL hash is now the
row's match key (m3u_key); stream_id stays what it was.
"""
import unittest

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import models.database as mdb
from models.database import LiveChannel, LiveChannelGroup, Provider
import routers.livetv as livetv
from services.m3u_parser import parse_m3u
from tmp_dirs import temp_dir


def _session():
    tmp = temp_dir()
    engine = create_engine(f"sqlite:///{tmp}/t.db")
    mdb.Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


def _m3u(token, n=30, host="panel", names=None):
    names = names or [f"Channel {i}" for i in range(n)]
    return "#EXTM3U\n" + "".join(
        f'#EXTINF:-1 tvg-id="c{i}" group-title="Sports",{name}\nhttp://{host}/live/u/p/{i}.ts?token={token}\n'
        for i, name in enumerate(names))


class M3uUrlChangeKeepsGuideNumber(unittest.TestCase):
    def setUp(self):
        self.db = _session()
        p = Provider(name="P", server_url="http://panel", username="u", password="p",
                     provider_type="m3u_url", live_tv_enabled=True)
        self.db.add(p)
        self.db.commit()
        self.pid = p.id
        self.db.add(LiveChannelGroup(provider_id=self.pid, name="Sports", enabled=True))
        self.db.commit()

    def tearDown(self):
        self.db.close()

    def _sync(self, text):
        livetv._upsert_channels_from_m3u(self.pid, parse_m3u(text), self.db)
        self.db.commit()
        self.db.expire_all()
        return {c.name: c for c in self.db.query(LiveChannel).filter_by(provider_id=self.pid)}

    def test_rotated_token_keeps_every_guide_number(self):
        first = self._sync(_m3u("aaa"))
        before = {n: c.stream_id for n, c in first.items()}
        rows_before = {n: c.id for n, c in first.items()}
        after = self._sync(_m3u("bbb"))
        self.assertEqual(set(before), set(after))
        self.assertEqual([], [n for n in before if before[n] != after[n].stream_id])
        self.assertEqual(rows_before, {n: c.id for n, c in after.items()})
        # ...and the new URL is the one proxied.
        self.assertTrue(all(c.stream_url.endswith("token=bbb") for c in after.values()))

    def test_a_second_and_third_rotation_still_match(self):
        before = {n: c.stream_id for n, c in self._sync(_m3u("aaa")).items()}
        for token in ("bbb", "ccc", "aaa", "ddd"):
            after = self._sync(_m3u(token))
            self.assertEqual(30, len(after))
            self.assertEqual(before, {n: c.stream_id for n, c in after.items()}, token)
            self.assertTrue(all(c.stream_url.endswith(f"token={token}") for c in after.values()))

    def test_user_settings_survive_the_rotation(self):
        rows = self._sync(_m3u("aaa"))
        rows["Channel 2"].enabled = False
        rows["Channel 2"].channel_number = 555
        self.db.commit()
        after = self._sync(_m3u("bbb", host="newhost:8080"))
        self.assertFalse(after["Channel 2"].enabled)
        self.assertEqual(555, after["Channel 2"].channel_number)

    def test_an_unchanged_playlist_changes_nothing(self):
        before = {n: (c.id, c.stream_id) for n, c in self._sync(_m3u("aaa")).items()}
        after = {n: (c.id, c.stream_id) for n, c in self._sync(_m3u("aaa")).items()}
        self.assertEqual(before, after)

    def test_a_real_new_channel_gets_a_new_number_and_a_removed_one_goes(self):
        before = {c.stream_id for c in self._sync(_m3u("aaa")).values()}
        names = [f"Channel {i}" for i in range(29)] + ["Brand New"]
        after = self._sync(_m3u("aaa", names=names))
        self.assertNotIn("Channel 29", after)
        self.assertIn("Brand New", after)
        self.assertNotIn(after["Brand New"].stream_id, before)

    def test_the_same_name_twice_is_left_to_add_and_remove_without_a_clash(self):
        names = ["Twin", "Twin"] + [f"Channel {i}" for i in range(28)]
        self._sync(_m3u("aaa", names=names))
        self._sync(_m3u("bbb", names=names))
        # Back to the first URLs: the twins' new rows must not collide with
        # a stream_id another row still holds.
        after_rows = self.db.query(LiveChannel).filter_by(provider_id=self.pid).all()
        self.assertEqual(30, len(after_rows))
        rows = self._sync(_m3u("aaa", names=names))
        self.assertEqual(28, len([n for n in rows if n != "Twin"]))
        ids = [c.stream_id for c in self.db.query(LiveChannel).filter_by(provider_id=self.pid)]
        self.assertEqual(len(ids), len(set(ids)))

    def test_rows_from_before_the_upgrade_match_by_their_stream_id(self):
        rows = self._sync(_m3u("aaa"))
        for c in rows.values():          # as an upgraded database has them
            c.m3u_key = None
        self.db.commit()
        before = {n: c.stream_id for n, c in rows.items()}
        after = self._sync(_m3u("aaa"))
        self.assertEqual(before, {n: c.stream_id for n, c in after.items()})
        after = self._sync(_m3u("bbb"))
        self.assertEqual(before, {n: c.stream_id for n, c in after.items()})


if __name__ == "__main__":
    unittest.main()
