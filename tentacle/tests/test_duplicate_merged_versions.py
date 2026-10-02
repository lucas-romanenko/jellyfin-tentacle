"""Duplicates: a film whose two copies share one folder (#333).

Jellyfin 10.11 shows "Heat (1995).strm" and "Heat (1995) - Bluray-1080p.mkv"
in one folder as ONE film with two versions: the listing has one Movie (the
file whose name is the folder's), with both copies as MediaSources; the other
version is its own item (type Video), returned only by /Items?Ids=. Users'
watched state is on the film (the UI and a version played from it), or on
the version's own item when a client reported that id.

Keep Downloaded looked for the download in the listing only, found nothing,
and refused every time a user had watched the film ("scan the library").

Measured on a real 10.11.8 after the .strm is deleted and the library
rescanned: the .mkv becomes a NEW Movie item; Jellyfin moves the users'
state of the deleted film onto it when it can tell it is the same film (an
NFO with its TMDB id beside the file) and drops it otherwise; state on the
old version item stays on that item, which nobody sees any more.

The fake Jellyfin answers like that 10.11.8.

Run from tentacle/:  python -m unittest tests.test_duplicate_merged_versions
"""
import random
import re
import unittest
from pathlib import Path

from fastapi import HTTPException

from models.database import Duplicate, Movie
from routers import duplicates
from tests.test_duplicate_keep_vod_merged_folder import _FakeArr
from tests.test_duplicate_keeps_user_data import FakeJellyfin, _Base, _Resp
from unittest import mock


class FakeJellyfin1011(FakeJellyfin):
    """Items may carry "Versions" (ids of the film's other versions, which are
    "Hidden": not in a type listing, only in an Ids= query)."""
    fail_ids = False

    def get(self, url, params=None, timeout=None):
        path = url.split("http://jf", 1)[1]
        if path == "/Items" and "Ids" in params:
            if self.down or self.fail_ids:
                raise ConnectionError("Jellyfin is down")
            wanted = params["Ids"].split(",")
            rows = []
            for i in self.items:
                if i["Id"] in wanted:
                    row = {k: v for k, v in i.items() if k not in ("Versions", "Hidden")}
                    if "MediaSources" in params.get("Fields", ""):
                        row["MediaSources"] = [{"Id": v} for v in [i["Id"]] + i.get("Versions", [])]
                    rows.append(row)
            return _Resp(200, {"Items": rows, "TotalRecordCount": len(rows)})
        if path == "/Items" and "ParentId" not in params:
            rows = [i for i in self.items if i["Type"] == params["IncludeItemTypes"] and not i.get("Hidden")]
            page = rows[params["StartIndex"]:params["StartIndex"] + params["Limit"]]
            return _Resp(200, {"Items": [{k: v for k, v in r.items() if k not in ("Versions", "Hidden")}
                                         for r in page], "TotalRecordCount": len(rows)})
        return super().get(url, params, timeout)


NFO = ('<?xml version="1.0" encoding="UTF-8"?>\n<movie>\n  <title>Heat</title>\n'
       '  <tmdbid>{}</tmdbid>\n</movie>\n')


def radarr_recycles_adopted_nfos(test):
    """Radarr adopts every <movie> NFO in a movie's folder as an extra of its
    file when it scans it, and deleting the file through the API (Keep VOD)
    recycles those extras too, Tentacle's .strm NFO included."""
    real = _FakeArr._delete_file

    def delete(arr, file_id):
        files = [Path(f["path"]) for t in arr.titles.values() for f in t["files"] if f["id"] == file_id]
        real(arr, file_id)
        for f in files:
            for nfo in f.parent.glob("*.nfo"):
                if "<movie>" in nfo.read_text():
                    nfo.unlink()
    _FakeArr._delete_file = delete
    test.addCleanup(setattr, _FakeArr, "_delete_file", real)


def jellyfin_reads_tmdb(film: Path) -> list:
    """The TMDB ids Jellyfin 10.11.8 takes from disk for a film alone in its
    folder: "[tmdbid-N]" in the folder name, else the file name
    (MovieResolver.SetProviderIdsFromPath), then the first NFO there of
    movie.nfo, <name>.nfo (MovieNfoSaver.GetMovieSavePaths)."""
    tag = re.compile(r"\[tmdbid[-=]([^\]]+)\]", re.I)
    m = tag.search(film.parent.name) or tag.search(film.name)
    ids = [m.group(1).strip()] if m else []
    for nfo in (film.parent / "movie.nfo", film.with_suffix(".nfo")):
        if nfo.exists():
            ids += re.findall(r"<tmdbid>\s*(\d+)\s*</tmdbid>", nfo.read_text())[:1]
            break
    return ids


class MergedFilm(_Base):
    def setUp(self):
        super().setUp()
        mock.patch("services.jellyfin.JellyfinService", FakeJellyfin1011).start()
        FakeJellyfin1011.fail_ids = False
        radarr_recycles_adopted_nfos(self)
        self.dir = self.root / "vod" / "movies" / "Heat (1995)"
        self.dir.mkdir(parents=True)
        self.strm = self.dir / "Heat (1995).strm"
        self.strm.write_text("http://p/movie/u/p/1.mp4")
        (self.dir / "Heat (1995).nfo").write_text(NFO.format(949))
        self.mkv = self.dir / "Heat (1995) - Bluray-1080p.mkv"
        self.mkv.write_bytes(b"\0" * 64)
        _FakeArr.titles[949] = {"id": 7, "path": str(self.dir), "files": [{"id": 70, "path": str(self.mkv)}]}
        self.db.add(Movie(tmdb_id=949, title="Heat", year="1995", source="provider_1", strm_path=str(self.strm),
                          radarr_path=str(self.mkv)))
        self.dup = Duplicate(tmdb_id=949, media_type="movie", resolution="pending",
                             sources=[{"source": "radarr", "path": str(self.mkv)},
                                      {"source": "provider_1", "path": str(self.strm)}])
        self.db.add(self.dup)
        self.db.commit()
        # Jellyfin mounts the folder elsewhere; the .strm is the film, the .mkv its hidden version.
        jf = "/data/vod-movies/Heat (1995)/"
        FakeJellyfin.items = [
            {"Id": "film", "Type": "Movie", "Path": jf + "Heat (1995).strm", "ProviderIds": {"Tmdb": "949"},
             "Versions": ["mkv"]},
            {"Id": "mkv", "Type": "Video", "Path": jf + "Heat (1995) - Bluray-1080p.mkv",
             "ProviderIds": {"Tmdb": "949"}, "Hidden": True},
            {"Id": "other", "Type": "Movie", "Path": "/data/movies/Other/Other.mkv", "ProviderIds": {"Tmdb": "1"}},
        ]

    def download_nfo(self, tmdb=949):
        (self.dir / "Heat (1995) - Bluray-1080p.nfo").write_text(NFO.format(tmdb))

    def keep(self, resolution):
        duplicates._apply_resolution(self.dup, resolution, self.db)

    def test_watched_film_with_the_downloads_nfo_is_resolved(self):
        # #333: was 409 "scan the library" for ever. The film's state is on the
        # film item, which Jellyfin re-attaches to the download by its TMDB id.
        self.download_nfo()
        FakeJellyfin.data = {("A", "film"): {"Played": True, "PlayCount": 1},
                             ("B", "film"): {"PlaybackPositionTicks": 6_000_000_000, "IsFavorite": True}}
        self.keep("keep_radarr")
        self.assertFalse(self.strm.exists())
        self.assertTrue(self.mkv.exists())
        self.assertEqual("radarr", self.db.query(Movie).one().source)
        self.assertEqual([], FakeJellyfin.posts, "the film's own state needs no copy")

    def test_state_on_the_hidden_version_goes_onto_the_film_first(self):
        # A client that played the version by its own id left the state there;
        # that item is left behind, unseen, after the rescan.
        self.download_nfo()
        FakeJellyfin.data = {("A", "film"): {"Played": True},
                             ("B", "mkv"): {"PlaybackPositionTicks": 6_000_000_000, "PlayCount": 1}}
        FakeJellyfin.on_post = lambda: self.assertTrue(self.strm.exists(), "deleted before the state moved")
        self.keep("keep_radarr")
        self.assertFalse(self.strm.exists())
        self.assertEqual({"PlaybackPositionTicks": 6_000_000_000, "PlayCount": 1}, FakeJellyfin.data[("B", "film")])
        self.assertEqual({"Played": True}, FakeJellyfin.data[("A", "film")])

    def rename_folder(self, name):
        new_dir = self.root / "vod" / "movies" / name
        self.dir.rename(new_dir)
        self.dir = new_dir
        self.strm, self.mkv = new_dir / self.strm.name, new_dir / self.mkv.name
        self.db.query(Movie).one().strm_path = str(self.strm)
        self.dup.sources = [{"source": "radarr", "path": str(self.mkv)}, {"source": "provider_1", "path": str(self.strm)}]
        self.db.commit()
        for i in FakeJellyfin.items:
            i["Path"] = i["Path"].replace("Heat (1995)/", name + "/")

    def test_folder_name_with_the_tmdb_id_identifies_the_download_too(self):
        self.rename_folder("Heat (1995) [tmdbid-949]")
        FakeJellyfin.data = {("A", "film"): {"Played": True}}
        self.keep("keep_radarr")
        self.assertFalse(self.strm.exists())

    def test_a_tmdb_id_without_brackets_does_not_count(self):
        self.rename_folder("Heat (1995) {tmdbid-949}")   # Jellyfin reads [tmdbid-N] / [tmdbid=N] only
        FakeJellyfin.data = {("A", "film"): {"Played": True}}
        with self.assertRaises(HTTPException):
            self.keep("keep_radarr")
        self.assertTrue(self.strm.exists())

    def test_movie_nfo_is_read_first(self):
        # Jellyfin reads movie.nfo before <name>.nfo: another film's id there wins
        self.download_nfo()
        (self.dir / "movie.nfo").write_text(NFO.format(1))
        FakeJellyfin.data = {("A", "film"): {"Played": True}}
        with self.assertRaises(HTTPException):
            self.keep("keep_radarr")
        self.assertTrue(self.strm.exists())

    def test_without_a_way_to_identify_the_download_a_watched_film_is_refused(self):
        FakeJellyfin.data = {("A", "mkv"): {"Played": True}, ("B", "film"): {"PlaybackPositionTicks": 6_000_000_000}}
        with self.assertRaises(HTTPException) as cm:
            self.keep("keep_radarr")
        self.assertEqual(409, cm.exception.status_code)
        self.assertIn("one film", cm.exception.detail)
        self.assertIn("Keep VOD keeps everyone's state", cm.exception.detail)
        self.assertNotIn("Scan the library", cm.exception.detail)
        self.assertTrue(self.strm.exists())
        self.assertEqual("provider_1", self.db.query(Movie).one().source)
        self.assertEqual([], FakeJellyfin.posts, "refused, yet users' state was changed")

    def test_an_nfo_naming_another_film_does_not_count(self):
        self.download_nfo(tmdb=1)
        FakeJellyfin.data = {("A", "mkv"): {"Played": True}}
        with self.assertRaises(HTTPException) as cm:
            self.keep("keep_radarr")
        self.assertEqual(409, cm.exception.status_code)
        self.assertTrue(self.strm.exists())

    def test_nobody_watched_it_proceeds_without_an_nfo(self):
        self.keep("keep_radarr")
        self.assertFalse(self.strm.exists())
        self.assertEqual([], FakeJellyfin.posts)

    def test_keep_vod_keeps_the_film_and_takes_the_downloads_version_state(self):
        FakeJellyfin.data = {("A", "film"): {"Played": True}, ("B", "mkv"): {"IsFavorite": True}}
        self.keep("keep_vod")
        self.assertTrue(self.strm.exists())
        self.assertFalse(self.mkv.exists())
        self.assertEqual({"IsFavorite": True}, FakeJellyfin.data[("B", "film")])

    def download_is_the_film(self):
        # Both named like the folder: Jellyfin may take the download for the film,
        # and the .strm's NFO is the one Tentacle keeps for both.
        same = self.dir / "Heat (1995).mkv"
        self.mkv.rename(same)
        self.mkv = same
        _FakeArr.titles[949]["files"] = [{"id": 70, "path": str(same)}]
        jf = FakeJellyfin.items[0]["Path"].rsplit("/", 1)[0] + "/"
        FakeJellyfin.items[0].update(Id="film", Type="Movie", Path=jf + "Heat (1995).mkv", Versions=["strm"])
        FakeJellyfin.items[1].update(Id="strm", Type="Video", Path=jf + "Heat (1995).strm")

    def test_keep_vod_when_the_download_is_the_film_is_refused(self):
        # Keep VOD removes the film item, and Radarr's file delete takes the
        # NFO with it: nothing would tell Jellyfin the .strm is this film.
        self.download_is_the_film()
        FakeJellyfin.data = {("A", "film"): {"Played": True}, ("B", "strm"): {"PlaybackPositionTicks": 5}}
        with self.assertRaises(HTTPException) as cm:
            self.keep("keep_vod")
        self.assertEqual(409, cm.exception.status_code)
        self.assertIn("Keep Downloaded keeps everyone's state", cm.exception.detail)
        self.assertTrue(self.mkv.exists() and self.strm.exists())
        self.assertEqual([], FakeJellyfin.posts)

    def test_keep_vod_when_the_download_is_the_film_with_the_id_in_the_folder_name(self):
        self.rename_folder("Heat (1995) [tmdbid-949]")
        self.download_is_the_film()
        FakeJellyfin.data = {("A", "film"): {"Played": True}, ("B", "strm"): {"PlaybackPositionTicks": 5}}
        self.keep("keep_vod")
        self.assertFalse(self.mkv.exists())
        self.assertEqual({"PlaybackPositionTicks": 5}, FakeJellyfin.data[("B", "film")])

    def test_keep_downloaded_when_the_download_is_the_film(self):
        self.download_is_the_film()
        FakeJellyfin.data = {("A", "film"): {"Played": True}, ("B", "strm"): {"PlaybackPositionTicks": 5}}
        self.keep("keep_radarr")   # the film item stays
        self.assertFalse(self.strm.exists())
        self.assertEqual({"PlaybackPositionTicks": 5}, FakeJellyfin.data[("B", "film")])

    def test_versions_lookup_failing_deletes_nothing(self):
        self.download_nfo()
        FakeJellyfin.data = {("A", "film"): {"Played": True}}
        FakeJellyfin1011.fail_ids = True
        with self.assertRaises(HTTPException) as cm:
            self.keep("keep_radarr")
        self.assertEqual(502, cm.exception.status_code)
        self.assertTrue(self.strm.exists())


class MergedFilmProperty(_Base):
    """Random merged/separate layouts, random state for 3 users, random
    failures: every resolution either deletes nothing, or leaves each user's
    state on an item that survives the rescan."""

    def setUp(self):
        super().setUp()
        mock.patch("services.jellyfin.JellyfinService", FakeJellyfin1011).start()
        radarr_recycles_adopted_nfos(self)

    def one(self, seed):
        rnd = random.Random(seed)
        root = self.root / f"s{seed}"
        merged, strm_is_film = rnd.random() < 0.7, rnd.random() < 0.7
        dl_nfo, nfo_tmdb = rnd.random() < 0.5, rnd.choice([949, 949, 1])
        keep = rnd.choice(["keep_radarr", "keep_vod"])
        folder = "Heat (1995)" + rnd.choice(["", "", "", " [tmdbid-949]", " {tmdbid-949}", " [tmdbid-1]"])
        same_stem = merged and rnd.random() < 0.3
        vdir = root / "vod" / folder
        ddir = vdir if merged else root / "movies" / folder
        vdir.mkdir(parents=True)
        ddir.mkdir(parents=True, exist_ok=True)
        strm = vdir / f"{folder}.strm"
        mkv = ddir / (f"{folder}.mkv" if same_stem else f"{folder} - Bluray-1080p.mkv")
        strm.write_text("http://p/movie/u/p/1.mp4")
        if rnd.random() < 0.9:
            strm.with_suffix(".nfo").write_text(NFO.format(949))
        mkv.write_bytes(b"\0")
        if dl_nfo and not same_stem:
            mkv.with_suffix(".nfo").write_text(NFO.format(nfo_tmdb))
        if rnd.random() < 0.15:
            (ddir / "movie.nfo").write_text(rnd.choice([NFO.format(949), NFO.format(1), "<movie></movie>"]))
        _FakeArr.titles, _FakeArr.calls = {949: {"id": 7, "path": str(ddir), "files": [{"id": 70, "path": str(mkv)}]}}, []
        for m in self.db.query(Movie).all():
            self.db.delete(m)
        for d in self.db.query(Duplicate).all():
            self.db.delete(d)
        self.db.commit()
        self.db.add(Movie(tmdb_id=949, title="Heat", year="1995", source="provider_1", strm_path=str(strm)))
        dup = Duplicate(tmdb_id=949, media_type="movie", resolution="pending",
                        sources=[{"source": "radarr", "path": str(mkv)}, {"source": "provider_1", "path": str(strm)}])
        self.db.add(dup)
        self.db.commit()
        vpath = f"/jf/vod/{folder}/{strm.name}"
        dpath = ("/jf/vod/" if merged else "/jf/movies/") + f"{folder}/{mkv.name}"
        if merged:
            film, ver = ("strm", vpath), ("mkv", dpath)
            if not strm_is_film:
                film, ver = ver, film
            items = [{"Id": film[0], "Type": "Movie", "Path": film[1], "ProviderIds": {"Tmdb": "949"}, "Versions": [ver[0]]},
                     {"Id": ver[0], "Type": "Video", "Path": ver[1], "ProviderIds": {"Tmdb": "949"}, "Hidden": True}]
        else:
            items = [{"Id": "strm", "Type": "Movie", "Path": vpath, "ProviderIds": {"Tmdb": "949"}},
                     {"Id": "mkv", "Type": "Movie", "Path": dpath, "ProviderIds": {"Tmdb": "949"}}]
        FakeJellyfin.items, FakeJellyfin.posts, FakeJellyfin.users = items, [], ["A", "B", "C"]
        FakeJellyfin.data = {}
        for u in FakeJellyfin.users:
            for i in items:
                if rnd.random() < 0.35:
                    FakeJellyfin.data[(u, i["Id"])] = rnd.choice([{"Played": True, "PlayCount": rnd.randint(1, 3)},
                                                                  {"PlaybackPositionTicks": rnd.randint(1, 9) * 10**9},
                                                                  {"IsFavorite": True}])
        FakeJellyfin.down = rnd.random() < 0.05
        FakeJellyfin1011.fail_ids = rnd.random() < 0.05
        before = {k: dict(v) for k, v in FakeJellyfin.data.items()}
        try:
            duplicates._apply_resolution(dup, keep, self.db)
            refused = False
        except HTTPException:
            refused = True
        removed = strm if keep == "keep_radarr" else mkv
        if refused:
            self.assertTrue(strm.exists() and mkv.exists(), f"seed {seed}: refused but a copy was deleted")
            return "refused"
        self.assertFalse(removed.exists(), f"seed {seed}")
        # What survives: separate folders = the kept item; merged = the film item
        # when it is the kept file, else the film item's state, which Jellyfin
        # moves to the new item by TMDB id: only if the files LEFT on disk give
        # the kept file that id.
        kept_file = mkv if keep == "keep_radarr" else strm
        ids = jellyfin_reads_tmdb(kept_file)
        identifiable = bool(ids) and set(ids) == {"949"}
        if merged:
            film_id = items[0]["Id"]
            film_is_kept = (film_id == "strm") == (keep == "keep_vod")
            survivors = [film_id] if (film_is_kept or identifiable) else []
        else:
            survivors = ["mkv" if keep == "keep_radarr" else "strm"]
        for u in FakeJellyfin.users:
            had = [v for (uu, _), v in before.items() if uu == u and v]
            if not had:
                continue
            self.assertTrue(survivors, f"seed {seed}: user {u} had state and nothing survives: {before}")
            now = FakeJellyfin.data.get((u, survivors[0]), {})
            for v in had:
                for k, val in v.items():
                    if k == "PlaybackPositionTicks" and now.get("Played"):
                        continue
                    if k == "PlaybackPositionTicks" and now.get(k):
                        continue   # the kept copy's own resume point wins (#297 rule)
                    self.assertTrue(now.get(k) and (now.get(k) >= val if k == "PlayCount" else True),
                                    f"seed {seed}: user {u} lost {k}={val}: before {before}, now {now}")
        return ("merged-" + ("film-kept" if film_is_kept else "film-removed")) if merged else "separate"

    def test_1000_seeds(self):
        seen = {self.one(seed) for seed in range(1000)}
        self.assertEqual({"refused", "separate", "merged-film-kept", "merged-film-removed"}, seen)


if __name__ == "__main__":
    unittest.main()
