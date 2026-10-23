"""Which versions run (services/versions.py, routers/about.py, /api/version).

Lucas (22 Oct 2026): "Why does tentacle show build ee28fde? Shouldn't it show
version?" The image now carries its release version (the tag, or for a build
of main the release it follows and how many commits since), Settings → About
shows the server, the plugin, Jellyfin and each TV app, and warns when the
plugin is older than this server needs. GitHub is never asked here: every
fetch is a fake.

Run from the tentacle/ directory:  python -m unittest tests.test_versions
"""
import json
import logging
import os
import unittest
from pathlib import Path
from unittest import mock

import requests
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import models.database as mdb
from services import family as F, tv_app, versions as V
from tmp_dirs import temp_dir

SHA = "ee28fde40ba7" + "0" * 28
ADMIN, ANN = "a" * 32, "b" * 32


class Resp:
    def __init__(self, status, body):
        self.status_code, self._body = status, body

    def json(self):
        return self._body


def _reset():
    V._derived.update(commit=None, version=None)
    V._latest.update(at=0.0, value=None)
    tv_app._cache.update(at=0.0, value=None)


class Case(unittest.TestCase):
    def setUp(self):
        logging.disable(logging.CRITICAL)
        self.addCleanup(logging.disable, logging.NOTSET)
        _reset()
        self.addCleanup(_reset)
        env = mock.patch.dict(os.environ, {"TENTACLE_VERSION": "", "TENTACLE_COMMIT": SHA})
        env.start()
        self.addCleanup(env.stop)


class Stamp(Case):
    def test_a_tag_or_git_describe_becomes_the_version_shown(self):
        for raw, shown in [("v1.23.0", "v1.23.0"), ("1.23.0", "v1.23.0"),
                           ("v1.23.0-3-gee28fde", "v1.23.0+3"), ("v1.23.0-0-gee28fde", "v1.23.0"),
                           ("v1.23.0+3", "v1.23.0+3"), ("v1.23.0-3-gee28fde-dirty", "v1.23.0+3"),
                           ("v2.0.0-rc.1", "v2.0.0-rc.1"), ("v2.0.0-rc.1-4-gabc1234", "v2.0.0-rc.1+4"),
                           (" v1.23.0\n", "v1.23.0")]:
            with self.subTest(raw=raw):
                self.assertEqual(shown, V.normalize(raw))

    def test_anything_else_is_no_version(self):
        # A plugin tag must never pass for the server's version.
        for raw in ("plugin-v2.286.0", "plugin-v2.286.0-1-gabc", "unknown", "", None, "main", "ee28fde"):
            with self.subTest(raw=raw):
                self.assertIsNone(V.normalize(raw))

    def test_release_and_changes_since(self):
        self.assertEqual("1.23.0", V.release_of("v1.23.0+3"))
        self.assertEqual(3, V.changes_since("v1.23.0+3"))
        self.assertEqual(0, V.changes_since("v1.23.0"))
        self.assertEqual(0, V.changes_since(None))

    def test_the_image_stamp_is_the_version(self):
        with mock.patch.dict(os.environ, {"TENTACLE_VERSION": "v1.23.0-3-gee28fde"}):
            self.assertEqual("v1.23.0+3", V.current())
        self.assertIsNone(V.current())

    def test_the_dockerfile_and_docker_publish_stamp_it(self):
        root = Path(__file__).resolve().parents[2]
        docker = (root / "tentacle" / "Dockerfile").read_text()
        self.assertIn("ARG TENTACLE_VERSION", docker)
        self.assertIn("ENV TENTACLE_VERSION=$TENTACLE_VERSION", docker)
        publish = (root / ".github" / "workflows" / "docker-publish.yml").read_text()
        self.assertIn("TENTACLE_VERSION=${{ github.event.release.tag_name || github.ref_name }}", publish)


class Derive(Case):
    """An image built without the stamp (straight from a GitHub URL at a
    commit, no tags to describe) asks GitHub once which release it follows."""

    def fetch(self, status="ahead", ahead=3, fail=False):
        calls = []

        def get(url):
            calls.append(url)
            if fail:
                raise requests.ConnectionError("offline")
            if "/releases?" in url:
                return Resp(200, RELEASES)
            return Resp(200, {"status": status, "ahead_by": ahead})
        return get, calls

    def test_a_build_of_main_after_a_release(self):
        get, calls = self.fetch()
        self.assertEqual("v1.23.0+3", V.derive(SHA, get))
        self.assertTrue(calls[1].endswith(f"/compare/v1.23.0...{SHA}"))
        self.assertEqual("v1.23.0+3", V.current())
        # asked once per commit
        V.derive(SHA, get)
        self.assertEqual(2, len(calls))

    def test_the_release_commit_itself(self):
        get, _ = self.fetch(status="identical", ahead=0)
        self.assertEqual("v1.23.0", V.derive(SHA, get))

    def test_a_commit_not_on_top_of_the_release_says_nothing(self):
        get, _ = self.fetch(status="diverged")
        self.assertIsNone(V.derive(SHA, get))
        self.assertIsNone(V.current())

    def test_offline_it_stays_unknown(self):
        get, _ = self.fetch(fail=True)
        self.assertIsNone(V.derive(SHA, get))
        self.assertIsNone(V.current())

    def test_no_commit_no_question(self):
        get, calls = self.fetch()
        self.assertIsNone(V.derive("unknown", get))
        self.assertEqual([], calls)

    def test_a_stamped_image_never_asks(self):
        with mock.patch.dict(os.environ, {"TENTACLE_VERSION": "v1.23.0"}), \
                mock.patch("threading.Thread") as thread:
            V.derive_in_background(SHA)
        thread.assert_not_called()


# The plugin's release is usually published last, so it heads the list (and is
# what /releases/latest would answer): the server's version is never taken from it.
RELEASES = [{"tag_name": "plugin-v2.286.0", "html_url": "https://example.invalid/p"},
            {"tag_name": "v1.24.0", "draft": True},
            {"tag_name": "v1.23.0", "html_url": "https://example.invalid/r"},
            {"tag_name": "v1.22.1", "html_url": "https://example.invalid/old"}]


class Latest(Case):
    def test_newest_release_cached_and_quiet_offline(self):
        calls = []

        def ok(url):
            calls.append(url)
            return Resp(200, [{"tag_name": "plugin-v2.290.0", "html_url": "https://example.invalid/p"},
                              {"tag_name": "v1.25.0-rc.1", "prerelease": True},
                              {"tag_name": "v1.24.0", "html_url": "https://example.invalid/v1.24.0"}])
        self.assertEqual({"version": "v1.24.0", "page": "https://example.invalid/v1.24.0"}, V.latest_release(ok))
        V.latest_release(ok)
        self.assertEqual(1, len(calls))
        _reset()

        def offline(url):
            raise requests.ConnectionError("offline")
        self.assertIsNone(V.latest_release(offline))

    def test_update_available(self):
        new = {"version": "v1.24.0"}
        self.assertTrue(V.update_available("v1.23.0+3", new))
        self.assertFalse(V.update_available("v1.24.0", new))
        self.assertFalse(V.update_available("v1.24.0+1", new))
        self.assertFalse(V.update_available(None, new))
        self.assertFalse(V.update_available("v1.23.0", None))
        self.assertTrue(V.update_available("v1.24.0-rc.2", new))


class Plugin(Case):
    def test_minimum_plugin_version(self):
        self.assertTrue(V.plugin_too_old("2.285.0.0"))
        self.assertFalse(V.plugin_too_old("2.286.0.0"))
        self.assertFalse(V.plugin_too_old("2.286.0.1511"))   # a private build of main
        self.assertFalse(V.plugin_too_old("2.300.0"))
        self.assertFalse(V.plugin_too_old(None))             # not installed is said otherwise
        self.assertTrue(V.plugin_too_old("2.99.0.0"))        # numbers, not text

    def test_the_catalogue_offers_the_minimum(self):
        # A minimum no released plugin meets would warn every install for ever.
        manifest = json.loads((Path(__file__).resolve().parents[2] / "tentacle-plugin" / "manifest.json").read_text())
        newest = max((v["version"] for v in manifest[0]["versions"]), key=V.plugin_key)
        self.assertLessEqual(V.plugin_key(V.MIN_PLUGIN_VERSION), V.plugin_key(newest))
        self.assertEqual(V.PLUGIN_ID, manifest[0]["guid"].replace("-", ""))

    def test_tv_app_features_waiting_for_the_server(self):
        self.assertEqual([], V.tv_app_waiting("1.25.2", "1.23.0", "2.286.0.0"))
        waiting = V.tv_app_waiting("1.25.2", "1.12.0", "2.286.0.0")
        self.assertEqual(["the On Now row on Home"], [n["what"] for n in waiting])
        self.assertEqual(["the On Now row on Home"],
                         [n["what"] for n in V.tv_app_waiting("1.25.0", "1.23.0", "2.276.0.0")])
        # an older TV app doesn't have the feature to miss
        self.assertEqual(["layout changes reach the TV while it is open"],
                         [n["what"] for n in V.tv_app_waiting("1.21.1", "1.9.0", "2.271.0.0")])
        self.assertEqual([], V.tv_app_waiting(None, "1.0.0", None))
        # a development server (no version) isn't held against it
        self.assertEqual([], V.tv_app_waiting("1.25.2", None, None))


class ApiVersion(Case):
    def test_version_is_in_api_version(self):
        import main
        c = TestClient(main.app)
        self.assertIsNone(c.get("/api/version").json()["version"])
        with mock.patch.dict(os.environ, {"TENTACLE_VERSION": "v1.23.0-3-gee28fde"}):
            body = c.get("/api/version").json()
        self.assertEqual("v1.23.0+3", body["version"])
        self.assertIn("commit", body)


class FakeJellyfin:
    def __init__(self, plugins=None, repositories=None, devices=None, down=False):
        self.plugins = plugins if plugins is not None else [
            {"Name": "Tentacle", "Version": "2.286.0.0", "Id": V.PLUGIN_ID, "Status": "Active"}]
        self.repositories = repositories if repositories is not None else [
            {"Name": "Tentacle", "Url": V.CATALOG_URL, "Enabled": True}]
        self.devices = devices or []
        self.down = down

    def get(self, path, **p):
        if self.down:
            raise requests.ConnectionError("down")
        return {"/System/Info": {"Version": "10.11.8", "ServerName": "home"},
                "/Plugins": self.plugins, "/Repositories": self.repositories,
                "/Devices": {"Items": self.devices}}[path]


class About(Case):
    def setUp(self):
        super().setUp()
        tmp = temp_dir(self)
        engine = create_engine(f"sqlite:///{tmp}/t.db")
        self.addCleanup(engine.dispose)
        mdb.Base.metadata.create_all(engine)
        self.db = sessionmaker(bind=engine)()
        self.addCleanup(self.db.close)
        self.admin = mdb.TentacleUser(jellyfin_user_id=ADMIN, display_name="Lucas", is_admin=True)
        self.db.add_all([self.admin, mdb.TentacleUser(jellyfin_user_id=ANN, display_name="Ann", is_admin=False)])
        self.db.commit()
        for target, value in (("services.versions.latest_release", {"version": "v1.24.0", "page": "https://example.invalid/v1.24.0"}),
                              ("services.tv_app.latest", {"version": "1.25.2", "url": "", "page": ""})):
            p = mock.patch(target, return_value=value)
            p.start()
            self.addCleanup(p.stop)

    def get(self, jf=None, error=None):
        from routers import about
        from routers.auth import require_admin
        app = FastAPI()
        app.include_router(about.router)
        app.dependency_overrides[mdb.get_db] = lambda: self.db
        app.dependency_overrides[require_admin] = lambda: self.admin
        side = F.FamilyError(*error) if error else None
        with mock.patch("services.family.jellyfin", return_value=jf, side_effect=side), \
                mock.patch.dict(os.environ, {"TENTACLE_VERSION": "v1.23.0-3-gee28fde",
                                             "TENTACLE_BUILD_DATE": "2026-10-22T18:00:00Z"}):
            r = TestClient(app).get("/api/about")
        self.assertEqual(200, r.status_code, r.text)
        return r.json()

    def test_every_version_and_a_newer_server(self):
        dev = [{"AppName": "Tentacle Android TV", "AppVersion": "1.24.0", "LastUserId": ANN},
               {"AppName": "Tentacle Android TV", "AppVersion": "1.25.2", "LastUserId": ADMIN}]
        d = self.get(FakeJellyfin(devices=dev))
        self.assertEqual("v1.23.0+3", d["server"]["version"])
        self.assertEqual(3, d["server"]["changes"])
        self.assertEqual(SHA, d["server"]["commit"])
        self.assertTrue(d["server"]["update"])
        self.assertEqual("https://example.invalid/v1.24.0", d["server"]["latest"]["page"])
        self.assertEqual("10.11.8", d["jellyfin"]["version"])
        self.assertEqual({"version": "2.286.0.0", "too_old": False, "catalog": True, "restart_for": None},
                         {k: d["plugin"][k] for k in ("version", "too_old", "catalog", "restart_for")})
        self.assertEqual([("Ann", "1.24.0", False), ("Lucas", "1.25.2", True)],
                         [(t["name"], t["version"], t["up_to_date"]) for t in d["tv_apps"]])
        self.assertEqual([], d["tv_apps"][1]["waiting"])
        self.assertIsNone(d["problem"])

    def test_a_plugin_older_than_the_server_needs_is_flagged(self):
        jf = FakeJellyfin(plugins=[
            {"Name": "Tentacle", "Version": "2.285.0.0", "Id": "b7e3f1a9-2c4d-4856-9a1b-8def23456789", "Status": "Active"},
            {"Name": "Tentacle", "Version": "2.286.0.0", "Id": V.PLUGIN_ID, "Status": "Restart"},
            {"Name": "Other", "Version": "9.0.0.0", "Id": "f" * 32, "Status": "Active"}],
            devices=[{"AppName": "Tentacle Android TV", "AppVersion": "1.25.2", "LastUserId": ANN}])
        d = self.get(jf)
        self.assertEqual("2.285.0.0", d["plugin"]["version"])
        self.assertTrue(d["plugin"]["too_old"])
        self.assertEqual(V.MIN_PLUGIN_VERSION, d["plugin"]["minimum"])
        # Jellyfin already installed the update: it runs after a restart
        self.assertEqual("2.286.0.0", d["plugin"]["restart_for"])

    def test_tv_app_features_the_plugin_cant_serve_yet(self):
        jf = FakeJellyfin(plugins=[{"Name": "Tentacle", "Version": "2.276.0.0", "Id": V.PLUGIN_ID, "Status": "Active"}],
                          devices=[{"AppName": "Tentacle Android TV", "AppVersion": "1.25.2", "LastUserId": ANN}])
        d = self.get(jf)
        self.assertEqual(["the On Now row on Home"], d["tv_apps"][0]["waiting"])

    def test_not_installed_and_no_catalogue(self):
        d = self.get(FakeJellyfin(plugins=[], repositories=[]))
        self.assertIsNone(d["plugin"]["version"])
        self.assertFalse(d["plugin"]["too_old"])
        self.assertFalse(d["plugin"]["catalog"])
        self.assertEqual(V.CATALOG_URL, d["plugin"]["catalog_url"])

    def test_jellyfin_down_still_says_the_server_version(self):
        d = self.get(FakeJellyfin(down=True))
        self.assertEqual("v1.23.0+3", d["server"]["version"])
        self.assertIsNone(d["plugin"])
        self.assertEqual("Jellyfin can't be reached right now.", d["problem"])

    def test_jellyfin_not_set_up_yet(self):
        d = self.get(error=("Jellyfin isn't connected yet (Settings → Services).", 409))
        self.assertIn("isn't connected", d["problem"])
        self.assertTrue(d["server"]["update"])

    def test_admins_only(self):
        from routers import about
        from routers.auth import require_admin
        self.assertIn(require_admin, [d.dependency for d in about.router.dependencies])


if __name__ == "__main__":
    unittest.main()
