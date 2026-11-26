"""Skip intro and Skip credits found by Tentacle (Nov 2026, Lucas: skipping
"seems to mostly not work well … Either never shows up to click skip or
shows up very late into end credits").

Run from the tentacle/ directory:  python -m unittest discover -s tests

- Chapters named like an intro or the credits are used as they are.
- Two episodes' opening fingerprints share the intro; unrelated sound
  (alike only bit by bit, never on average) doesn't count; too short or too
  long a shared stretch isn't an intro.
- Credits from the end fingerprints, in the episode's own time; credits on
  black when the sound says nothing.
- Starting an episode queues it, the one before it and the next ones.
- The plugin's provider reads times only, per item.
"""
import random
import unittest

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from tmp_dirs import temp_dir


def _db(test):
    import models.database as mdb
    engine = create_engine(f"sqlite:///{temp_dir(test)}/t.db", connect_args={"check_same_thread": False})
    mdb.Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


def _noise(n, seed):
    r = random.Random(seed)
    return [r.getrandbits(32) for _ in range(n)]


def _alike_noise(n, seed):
    """Unrelated sound that chromaprint makes into points ~6 bits apart from another's."""
    r = random.Random(seed)
    base = _noise(n, 99)
    out = []
    for v in base:
        for _ in range(6):
            v ^= 1 << r.randrange(32)
        out.append(v)
    return out


def _flip(points, bits, seed):
    r = random.Random(seed)
    out = []
    for v in points:
        for _ in range(bits):
            v ^= 1 << r.randrange(32)
        out.append(v)
    return out


class Chapters(unittest.TestCase):
    def test_named_chapters(self):
        from services.segment_detect import from_chapters
        got = from_chapters([(0, 90, "Cold Open"), (90, 150, "Opening Titles"), (150, 2500, "Chapter 3"),
                             (2500, 2640, "End Credits")], 2640)
        self.assertEqual(got, {"intro": (90, 150), "credits": (2500, 2640)})
        self.assertEqual(from_chapters([(0, 600, "Chapter 1"), (600, 1200, "Chapter 2")], 1200), {})
        self.assertEqual(from_chapters([(0, 40, "Intro"), (40, 0, "Credits")], 900), {"intro": (0, 40), "credits": (40, 900)})


class SharedSound(unittest.TestCase):
    def setUp(self):
        from services import segment_detect
        self.D = segment_detect
        per_s = 1 / segment_detect.POINT_S
        self.intro = _noise(int(30 * per_s), 1)                      # 30 s of the same tune
        self.a = _noise(int(10 * per_s), 2) + self.intro + _noise(int(120 * per_s), 3)
        self.b = _noise(int(45 * per_s), 4) + _flip(self.intro, 1, 5) + _noise(int(90 * per_s), 6)

    def test_the_intro_both_share(self):
        start, end = self.D.intro_between(self.a, self.b)
        self.assertAlmostEqual(start, 10 + self.D.START_PAD_S, delta=0.4)
        self.assertAlmostEqual(end, 40, delta=0.6)      # a skip may land a moment early, never late
        start, end = self.D.intro_between(self.b, self.a)
        self.assertAlmostEqual(start, 45 + self.D.START_PAD_S, delta=0.4)

    def test_unrelated_sound_alike_bit_by_bit_is_not_an_intro(self):
        n = len(self.a)
        self.assertIsNone(self.D.intro_between(_noise(n, 7), _noise(n, 8)))
        # ~6 bits apart everywhere: every point "alike", the average never is
        self.assertIsNone(self.D.intro_between(_noise(n, 99), _alike_noise(n, 9)))

    def test_too_short_or_too_long_is_not_an_intro(self):
        per_s = 1 / self.D.POINT_S
        short = _noise(int(8 * per_s), 10)
        self.assertIsNone(self.D.intro_between(_noise(300, 11) + short + _noise(300, 12),
                                               _noise(200, 13) + short + _noise(400, 14)))
        same = _noise(int(200 * per_s), 15)
        self.assertIsNone(self.D.intro_between(same, same))

    def test_an_intro_at_the_very_start_starts_at_zero(self):
        a = self.intro + _noise(500, 16)
        b = self.intro + _noise(500, 17)
        self.assertEqual(self.D.intro_between(a, b)[0], 0.0)

    def test_credits_in_the_episodes_own_time(self):
        per_s = 1 / self.D.POINT_S
        music = _noise(int(40 * per_s), 20)
        tail_a = _noise(int(60 * per_s), 21) + music
        tail_b = _noise(int(80 * per_s), 22) + music
        start, end = self.D.credits_between(tail_a, 2400.0, tail_b)
        self.assertAlmostEqual(start, 2460 + self.D.START_PAD_S, delta=0.5)
        self.assertAlmostEqual(end, 2500, delta=0.5)
        self.assertEqual(self.D.credits_end((start, 2497.0), 2500.0), (start, 2500.0))
        self.assertEqual(self.D.credits_end((start, 2480.0), 2500.0), (start, 2480.0))   # a scene after the credits

    def test_credits_on_black(self):
        frames = [(t, 10) for t in range(2300, 2440, 4)] + [(t, 97) for t in range(2440, 2500, 4)]
        self.assertEqual(self.D.credits_from_black(frames, 2500.0), (2440, 2500.0))
        self.assertIsNone(self.D.credits_from_black([(t, 10) for t in range(2300, 2500, 4)], 2500.0))
        # a dark scene well before the end, then the episode goes on: not credits
        self.assertIsNone(self.D.credits_from_black([(t, 97) for t in range(2300, 2340, 4)]
                                                    + [(t, 10) for t in range(2340, 2500, 4)], 2500.0))


class Queue(unittest.TestCase):
    def test_a_play_queues_the_one_before_and_the_next(self):
        from models.database import SegmentJob
        from services import segments
        db = _db(self)
        eps = [{"Id": f"E{i}", "Type": "Episode", "SeriesName": "Show", "ParentIndexNumber": 1, "IndexNumber": i,
                "SeasonId": "S1"} for i in range(1, 12)]
        n = segments.on_play(db, eps[3], eps)
        jobs = {j.jellyfin_item_id: j for j in db.query(SegmentJob).all()}
        self.assertEqual(n, 1 + 1 + segments.SEASON_AHEAD)
        self.assertEqual(sorted(jobs), sorted(f"e{i}" for i in range(3, 4 + segments.SEASON_AHEAD + 1)))
        self.assertEqual(jobs["e4"].priority, segments.WATCHING)
        self.assertEqual(jobs["e4"].season_id, "s1")
        self.assertEqual(segments.on_play(db, {"Id": "M1", "Type": "Movie"}, []), 0)

    def test_the_provider_reads_times_per_item(self):
        from services import segments
        db = _db(self)
        segments._save(db, "abc", "intro", (8.0, 38.5), "audio")
        segments._save(db, "abc", "credits", (2400.0, 2500.0), "black")
        segments._save(db, "other", "intro", (1.0, 20.0), "audio")
        self.assertEqual(segments.for_item(db, "ABC"), [
            {"type": "Intro", "start_ms": 8000, "end_ms": 38500, "source": "audio"},
            {"type": "Outro", "start_ms": 2400000, "end_ms": 2500000, "source": "black"}])

    def test_chapters_are_not_overwritten_by_a_guess(self):
        from services import segments
        db = _db(self)
        self.assertTrue(segments._save(db, "abc", "intro", (8.0, 38.0), "chapters"))
        self.assertFalse(segments._save(db, "abc", "intro", (7.0, 37.0), "audio"))
        self.assertFalse(segments._save(db, "abc", "intro", (8.1, 38.2), "chapters"))      # the same, within 0.5 s
        self.assertEqual(segments.for_item(db, "abc")[0]["start_ms"], 8000)


if __name__ == "__main__":
    unittest.main()
