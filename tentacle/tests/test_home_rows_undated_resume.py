"""An episode with a resume point but no last-played date stays on the home (#920).

Jellyfin 10.11's GetNextUp with EnableResumable = false returns nothing for a show
whose next episode has PlaybackPositionTicks > 0 (TVSeriesManager.GetNextUp), and
Continue Watching's resume list is ordered by DatePlayed and cut at the row's
limit. A position saved without a playback (no LastPlayedDate, PlayCount 0) sorts
last and is cut, so the show was on neither row while Jellyfin's own Next Up
listed it. Next Up is asked with EnableResumable = true and leaves only the
*dated* in-progress episodes to the resume list; undated resume items come after
Next Up, so such an episode takes its show's last-watched date.

There is no C# test host here, so this reads the controller source.

Run from the tentacle/ directory:  python -m unittest tests.test_home_rows_undated_resume
"""
import re
import unittest
from pathlib import Path

API = Path(__file__).resolve().parents[2] / "tentacle-plugin" / "Api"


def _body(src: str, signature: str) -> str:
    start = re.search(signature, src).start()
    nxt = re.search(r"\n    (///|\[Http|private |public )", src[start + 1:])
    return src[start:start + 1 + nxt.start()] if nxt else src[start:]


class UndatedResume(unittest.TestCase):
    def setUp(self):
        self.src = (API / "HomeScreenController.cs").read_text(encoding="utf-8")
        self.next_episodes = _body(self.src, r"private List<\(BaseItem Item, DateTime Played\)> NextEpisodes\(")
        self.continue_watching = _body(self.src, r"public async Task<ActionResult> GetContinueWatching\(")

    def test_next_up_keeps_a_show_whose_next_episode_has_a_position(self):
        self.assertIn("EnableResumable = true", self.next_episodes,
                      "EnableResumable = false drops the whole show when its next episode has a position")

    def test_next_up_leaves_only_dated_in_progress_episodes_to_the_resume_list(self):
        skip = re.search(r"GetUserData\(user, next\) is \{([^}]*)\}\s*\)\s*\{\s*continue;", self.next_episodes)
        self.assertIsNotNone(skip, "an in-progress next episode the resume list places must be skipped")
        self.assertIn("PlaybackPositionTicks: > 0", skip.group(1))
        self.assertIn("LastPlayedDate: not null", skip.group(1))

    def test_undated_resume_items_come_after_next_up(self):
        body = self.continue_watching
        self.assertNotIn("LastPlayedDate ?? DateTime.MinValue", body,
                         "an undated resume item must not take MinValue ahead of its show's Next Up card")
        next_up = body.index("NextEpisodes(user, dtoOptions)")
        undated = re.search(r"Played is null\)", body[next_up:])
        self.assertIsNotNone(undated, "undated resume items are added after Next Up")


if __name__ == "__main__":
    unittest.main()
