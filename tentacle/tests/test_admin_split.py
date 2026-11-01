"""Admin things live under Server, never on the pages everyone uses (Lucas, 31 Oct 2026).

Library, Discover, My Home Screen, My Channels, Recordings and My YouTube are
every family member's pages: they show only what a normal person needs.
Managing Radarr/Sonarr, the duplicates rule, space limits, Lidarr fixes and the
like are on the Server pages (People, Requests, Sources, Health, Settings),
which only admins see. Hiding a control from non-admins on a family page isn't
enough: it moves to Server, so the family pages stay simple for admins too.

Run from tentacle/:  python -m unittest tests.test_admin_split
"""
import re
import unittest
from pathlib import Path
from unittest import mock

STATIC = Path(__file__).resolve().parents[1] / "static"
HTML = (STATIC / "index.html").read_text(encoding="utf-8")
PAGES_JS = (STATIC / "js" / "pages.js").read_text(encoding="utf-8")
MUSIC_JS = (STATIC / "js" / "music.js").read_text(encoding="utf-8")

FAMILY_PAGES = ["library", "discover", "jellyfin", "mychannels", "recordings", "myyoutube"]

# data-admin-only left on a family page, each waiting for Lucas's call.
# Add nothing here without his yes: move the control to a Server page instead.
ALLOWED_ADMIN_ONLY = {
    # "Mine / Everyone's default" home switch: the household's default home
    # uses the same editor (RESULT.md of tentacle-admin-split asks him).
    "jellyfin": 1,
}


def _page(name: str) -> str:
    """The markup of one page, up to the next page."""
    starts = [(m.start(), m.group(1)) for m in re.finditer(r'<div class="page(?: active)?" id="page-([\w-]+)">', HTML)]
    for i, (pos, pid) in enumerate(starts):
        if pid == name:
            end = starts[i + 1][0] if i + 1 < len(starts) else len(HTML)
            return HTML[pos:end]
    raise AssertionError(f"no page-{name} in index.html")


def _element(eid: str) -> str:
    """From the element with this id to the end of its page."""
    i = HTML.index(f'id="{eid}"')
    nxt = re.search(r'<div class="page(?: active)?" id="page-', HTML[i:])
    return HTML[i:i + nxt.start()] if nxt else HTML[i:]


class FamilyPagesHaveNoAdminControls(unittest.TestCase):
    def test_no_admin_only_markup_on_family_pages(self):
        for name in FAMILY_PAGES:
            n = _page(name).count("data-admin-only")
            self.assertLessEqual(n, ALLOWED_ADMIN_ONLY.get(name, 0),
                                 f"page-{name} has {n} data-admin-only control(s): move them to a Server page")

    def test_not_in_radarr_sonarr_is_under_sources(self):
        self.assertNotIn("untracked", _page("library"))
        self.assertIn('id="untracked-card"', _page("sources"))
        self.assertIn('id="untracked-list"', _element("sources-tab-arr"))
        self.assertIn('href="#sources/arr" id="setup-untracked-open"', HTML)

    def test_recording_space_limit_is_under_sources(self):
        rec = _page("recordings")
        self.assertNotIn('id="rec-space"', rec)
        self.assertNotIn("recSpaceOpen", rec)
        self.assertIn('id="rec-space-row"', _element("live-panel-more"))

    def test_music_fix_library_is_under_sources(self):
        self.assertNotIn('data-musicfilter="fix"', _page("library"))
        self.assertIn('id="music-fix-card"', _element("sources-tab-music"))
        self.assertNotIn("resolveMusicReview", MUSIC_JS.split("function _musicActions", 1)[1].split("\n}\n", 1)[0])

    def test_music_album_lists_are_picked_under_sources(self):
        lists_view = MUSIC_JS.split("function _musicLists", 1)[1].split("\n}\n", 1)[0]
        self.assertNotIn("addMusicList", lists_view)
        self.assertNotIn("removeMusicList", lists_view)
        self.assertIn('id="music-lists-card"', _element("sources-tab-music"))

    def test_title_details_have_no_duplicates_switches(self):
        # Prefer the download (#872) and Manage .strm files are lists on Health → Duplicates now
        self.assertNotIn('onchange="setPreferDownload(', PAGES_JS)
        self.assertNotIn('onchange="toggleStrmManaged(', PAGES_JS)
        self.assertIn("dupTitleAdd", PAGES_JS)

    def test_old_library_link_lands_on_sources(self):
        app = (STATIC / "js" / "app.js").read_text(encoding="utf-8")
        self.assertIn("if (name === 'library' && tab === 'untracked') { name = 'sources'; tab = 'arr'; }", app)


class StrmUnmanagedList(unittest.TestCase):
    """GET /api/library/strm-managed: the titles Tentacle writes no .strm for (admins)."""

    def _client(self, admin: bool):
        from fastapi.testclient import TestClient
        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker
        from sqlalchemy.pool import StaticPool
        import main
        import models.database as mdb
        import routers.auth as auth

        engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
        mdb.Base.metadata.create_all(engine)
        Session = sessionmaker(bind=engine, expire_on_commit=False)
        db = Session()
        user = mdb.TentacleUser(jellyfin_user_id="jf-u", display_name="someone", is_admin=admin)
        db.add(user)
        db.add(mdb.Movie(tmdb_id=949, title="Heat", source="provider_1", strm_disabled=True))
        db.add(mdb.Movie(tmdb_id=680, title="Pulp Fiction", source="provider_1", strm_disabled=False))
        db.add(mdb.Series(tmdb_id=1396, title="Breaking Bad", source="provider_1", strm_disabled=True))
        db.commit()

        def _db():
            s = Session()
            try:
                yield s
            finally:
                s.close()

        main.app.dependency_overrides[mdb.get_db] = _db
        self.addCleanup(main.app.dependency_overrides.clear)
        patcher = mock.patch.object(auth, "get_user_from_request", return_value=user)
        patcher.start()
        self.addCleanup(patcher.stop)
        return TestClient(main.app, raise_server_exceptions=False)

    def test_admin_gets_the_opted_out_titles(self):
        r = self._client(admin=True).get("/api/library/strm-managed")
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json(), [
            {"media_type": "series", "tmdb_id": 1396, "title": "Breaking Bad"},
            {"media_type": "movie", "tmdb_id": 949, "title": "Heat"},
        ])

    def test_non_admin_is_refused(self):
        r = self._client(admin=False).get("/api/library/strm-managed")
        self.assertEqual(r.status_code, 403, r.text)


if __name__ == "__main__":
    unittest.main()
