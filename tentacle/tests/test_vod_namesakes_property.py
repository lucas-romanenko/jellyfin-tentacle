"""#185 property test: random small VOD libraries, provider id shapes and TMDB
failures, checked against invariants over two consecutive syncs.

Each seed builds a world:
- films with namesakes (same title and year), remakes (+-1 year), long and
  multi-byte names, names that sanitise alike;
- Radarr-owned films, in the merged layout (folder inside the VOD root, with
  or without an NFO) or elsewhere;
- two providers with overlapping catalogues, some films listed twice;
- per-stream provider ids: right, wrong (another film), junk, unicode digits,
  ids TMDB answers 404 for, odd types;
- streams renumbered at the upgrade, and TMDB up, down or rate-limited (by id).

Night 0 is the library as it was before (provider ids not read: they are
stripped). Nights 1-3 (1-5 when files are deleted) run with the ids, in the
same world. Invariants (CONTRACT-185.md section 2):
  S1  no path of a row from night 0 moves, and no night-0 row or file is lost
  S2  no folder holds files of two different films (unless it already did)
  S3  every sync completes (never raises)
  S4  the night after a clean night changes nothing (files, contents, rows)
  S5  no stream plays in two rows (unless it already did), by stream identity
  S6  TMDB calls on the last night stay within a bound
  R1a a row that played its own film still does (while its file exists)
  R1b a new or changed row plays its own film, unless that stream's own
      provider id mislabels it
  R2  a plain import next to an unmanaged video keeps 755ea67's folder
  O1  a row changes owner only on a plain name match, never on a provider id
  R1c (reported only) a row plays a mislabelled stream while a right one exists

TENTACLE_FUZZ_SEEDS (default 12) and TENTACLE_FUZZ_FIRST (default 0) pick the
seeds; the scratch runner used for the PR ran 300+. A failing seed is kept in
REGRESSION_SEEDS. Self-contained: no network.
"""
import collections
import hashlib
import json
import os
import random
import shutil
import sys
import tempfile
import unittest
from pathlib import Path as _RealPath

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import models.database as mdb
from models.database import Movie, Provider, ProviderCategory
import services.sync as sync
from services.tmdb import TMDBConnectionError

# Seeds that found a violation during development (kept so they run every time):
# 42 60 189 (a renumbered namesake stream re-matched and the old row was pruned),
# 80 137 156 222 (night 2 re-decided a namesake), 147 (a deferred decision with TMDB
# rate-limited merged a tag; found in the upgrade runner, 755ea67 night 0).
# 654 (a heal rewrote a .strm two night-0 rows already shared; upgrade runner).
REGRESSION_SEEDS = [42, 60, 80, 137, 147, 156, 189, 222, 654]

POOL = ["Brothers", "Home", "Heat", "Crash", "The Guest", "Nomad", "AC/DC Live", "ACDC Live",
        "...And Justice", "Léon", "Amélie", "東京物語", "アニメ" * 40, "Long " + "x" * 190]


def make_world(seed, v2=True):
    rnd = random.Random(seed)
    films = {}  # tmdb id -> (title, year)
    next_id = [1000]

    def film(title, year):
        next_id[0] += rnd.randint(1, 50)
        films[next_id[0]] = (title, str(year))
        return next_id[0]

    for _ in range(rnd.randint(4, 9)):
        t, y = rnd.choice(POOL), rnd.choice([1999, 2008, 2024])
        film(t, y)
        r = rnd.random()
        if r < 0.35:
            film(t, y)                      # a namesake
        elif r < 0.5:
            film(t, int(y) + rnd.choice((-1, 1)))  # a remake the year after/before
    ids = sorted(films)
    # Name search: for namesakes TMDB returns one of them, always the same one.
    search = {}
    for tid in ids:
        key = (films[tid][0].lower(), films[tid][1])
        if key not in search or rnd.random() < 0.5:
            search[key] = tid
    not_on_tmdb = {tid for tid in ids if rnd.random() < 0.08}
    radarr = {}  # tmdb id -> "merged-nfo" | "merged" | "elsewhere"
    for tid in ids:
        if rnd.random() < 0.15:
            radarr[tid] = rnd.choice(["merged-nfo", "merged", "elsewhere"])

    providers = []
    for p in range(2):
        streams = []
        for tid in ids:
            if rnd.random() < (0.85 if p == 0 else 0.35):
                for listing in range(1 if rnd.random() < 0.8 else 2):
                    streams.append({"tid": tid, "sid": rnd.randint(1, 10 ** 6), "cat": rnd.choice("ab"),
                                    "hint": pick_hint(rnd, tid, ids)})
        rnd.shuffle(streams)
        providers.append({"streams": streams, "require_tmdb": rnd.random() < 0.8,
                          "renumber": rnd.random() < 0.25})
    tmdb_mode = rnd.choices(["up", "down", "429"], [0.7, 0.15, 0.15])[0]
    world = {"films": films, "search": search, "not_on_tmdb": not_on_tmdb, "radarr": radarr,
             "providers": providers, "tmdb_mode": tmdb_mode, "seed": seed, "v2": v2,
             "unmanaged": [], "shuffle": False, "fault": None, "crash_after": 0}
    if v2:
        add_v2(world, random.Random(seed * 7919 + 17))
    return world


def add_v2(world, rnd):
    """Contract-185 events on top of the v1 world (a separate random stream, so
    v1 worlds - and their regression seeds - are unchanged):
    - per provider, how its ids are made: v1 mix / the panel's own name search
      (namesakes share one id) / right ids with every namesake pair SWAPPED /
      all right;
    - a provider host change after night 0;
    - unmanaged videos (no Radarr row, no NFO) in VOD-named folders;
    - stream order shuffled every night;
    - one fault on night 1: a category fetch fails, or the process is killed."""
    films, search = world["films"], world["search"]
    for prov in world["providers"]:
        mode = rnd.choices(["mixed", "panel", "swap", "right"], [0.5, 0.2, 0.15, 0.15])[0]
        prov["mode"] = mode
        for s in prov["streams"]:
            t, y = films[s["tid"]]
            if mode == "panel":
                s["hint"] = search.get((t.lower(), y))
            elif mode in ("swap", "right"):
                s["hint"] = s["tid"]
        if mode == "swap":
            by_name = {}
            for s in prov["streams"]:
                by_name.setdefault(films[s["tid"]], []).append(s)
            for group in by_name.values():
                tids = sorted({s["tid"] for s in group})
                if len(tids) >= 2:
                    a, b = tids[0], tids[1]
                    for s in group:
                        s["hint"] = b if s["tid"] == a else a if s["tid"] == b else s["hint"]
        prov["host_change"] = rnd.random() < 0.2
    world["unmanaged"] = [tid for tid in sorted(films) if tid not in world["radarr"] and rnd.random() < 0.1]
    world["shuffle"] = rnd.random() < 0.5
    world["fault"] = rnd.choices([None, "cat_fail", "crash"], [0.8, 0.1, 0.1])[0]
    world["crash_after"] = rnd.randint(1, 15)
    # Design-review additions (CONTRACT-185 section 10):
    # - two providers as two accounts on one panel host (E27);
    # - a namesake row's .strm deleted before night 2 (E25);
    # - cross-provider claims: a stream of X whose id names a namesake H that
    #   another provider carries (E26).
    world["same_host"] = rnd.random() < 0.2
    world["drop_strm"] = rnd.random() < 0.15
    if rnd.random() < 0.3:
        names = {}
        for tid, key in films.items():
            names.setdefault(key, []).append(tid)
        for p, prov in enumerate(world["providers"]):
            other = world["providers"][1 - p]
            for group in names.values():
                for x in group:
                    for h in group:
                        if x != h and any(s["tid"] == h for s in other["streams"]) and rnd.random() < 0.5:
                            prov["streams"].append({"tid": x, "sid": rnd.randint(1, 10 ** 6), "cat": rnd.choice("ab"),
                                                    "hint": str(h), "cross": True})


def pick_hint(rnd, tid, ids):
    kind = rnd.choices(["right", "none", "wrong", "junk", "unicode", "404", "odd"],
                       [0.45, 0.15, 0.12, 0.08, 0.05, 0.08, 0.07])[0]
    if kind == "right":
        return rnd.choice([tid, str(tid), f" {tid} "])
    if kind == "none":
        return None
    if kind == "wrong":
        return str(rnd.choice(ids))
    if kind == "junk":
        return rnd.choice(["tt0111161", "abc", "-5", "0", "", "1e3"])
    if kind == "unicode":
        return rnd.choice(["²", "٣٤", "１２３", f"{tid}²"])
    if kind == "404":
        return str(90_000_000 + rnd.randint(1, 1000))
    return rnd.choice([True, [tid], float(tid), {"id": tid}])


class _TMDB:
    world = None
    night = 0
    calls = {"search": 0, "details": 0, "failed": 0}

    def __init__(self, *a, **k):
        self.enabled = True

    def _down(self, tid_or_key):
        mode = _TMDB.world["tmdb_mode"] if _TMDB.night > 0 else "up"
        if mode == "down":
            return True
        if mode == "429":
            h = int(hashlib.md5(repr(tid_or_key).encode()).hexdigest(), 16)
            return h % 10 < 3
        return False

    def _meta(self, tid):
        t, y = _TMDB.world["films"][tid]
        return {"tmdb_id": tid, "title": t, "year": y, "overview": "", "genres": [],
                "poster_path": None, "backdrop_path": None, "rating": None, "runtime": None}

    def search_movie(self, name, year=None, strict=False, **k):
        _TMDB.calls["search"] += 1
        if self._down((name.lower(), year)):
            _TMDB.calls["failed"] += 1
            if strict:
                raise TMDBConnectionError("down")
            return None
        tid = _TMDB.world["search"].get((name.lower(), year))
        if tid is None or tid in _TMDB.world["not_on_tmdb"]:
            return None
        return self._meta(tid)

    def get_movie_details(self, tid, **k):
        _TMDB.calls["details"] += 1
        if not isinstance(tid, int):
            return None
        if self._down(tid):
            _TMDB.calls["failed"] += 1
            return None
        if tid in _TMDB.world["films"] and tid not in _TMDB.world["not_on_tmdb"]:
            return self._meta(tid)
        return None

    def search_series(self, *a, **k):
        return None

    def get_series_details(self, *a, **k):
        return None

    def cleanup_cache(self):
        pass


class _Client:
    def __init__(self, world, p, host, night):
        self.world, self.p, self.host, self.night = world, p, host, night

    def get_vod_streams(self, cat):
        prov = self.world["providers"][self.p]
        if self.world["fault"] == "cat_fail" and self.night == 1 and self.p == 0 and cat == "b":
            raise ConnectionError("injected: category fetch failed")
        out = []
        for s in prov["streams"]:
            if s["cat"] != cat:
                continue
            t, y = self.world["films"][s["tid"]]
            sid = s["sid"] + (5_000_000 if prov["renumber"] and self.night > 0 else 0)
            d = {"name": f"{t} ({y})", "stream_id": sid, "container_extension": "mkv"}
            if self.night > 0 and s["hint"] is not None:
                d["tmdb"] = s["hint"]
            out.append(d)
        if self.world["shuffle"] and self.night > 0:
            random.Random(self.world["seed"] * 31 + self.night * 7 + self.p).shuffle(out)
        return out

    def movie_stream_url(self, sid, ext):
        return f"http://{self.host}/movie/{user_of(self.world, self.p)}/p/{sid}.{ext}"

    def get_series_list(self, cat):
        return []

    def get_series_info(self, sid):
        return {"episodes": {}}

    def episode_stream_url(self, e, ext):
        return f"http://{self.host}/series/u/p/{e}.{ext}"


class _Killed(BaseException):
    pass


def host_base(world, p):
    return "host0" if world.get("same_host") and p == 1 else f"host{p}"


def user_of(world, p):
    return "u2" if world.get("same_host") and p == 1 else "u"


def host_of(world, p, night):
    base = host_base(world, p)
    return f"{base}b" if world["providers"][p].get("host_change") and night > 0 else base


def stream_index(world, night):
    """url -> (provider, stream index, true film, id the provider sends) for the
    streams that exist on `night`, in either host form (a host change is the same
    stream at a new address). A URL with a number the provider no longer uses
    (renumbered) is a dead file: it plays nothing, and is not in the index."""
    out = {}
    for p, prov in enumerate(world["providers"]):
        for i, s in enumerate(prov["streams"]):
            sid = s["sid"] + (5_000_000 if prov["renumber"] and night > 0 else 0)
            base = host_base(world, p)
            for host in {base, f"{base}b"}:
                out[f"http://{host}/movie/{user_of(world, p)}/p/{sid}.mkv"] = (p, i, s["tid"], s["hint"])
    return out


class World:
    """One seed's library on disk; runs nights with whatever services.sync is loaded."""

    def __init__(self, seed, root, v2=True):
        self.world = make_world(seed, v2)
        self.root = _RealPath(root)
        self.vod = self.root / "vod"
        (self.vod / "movies").mkdir(parents=True, exist_ok=True)
        (self.vod / "shows").mkdir(parents=True, exist_ok=True)
        self.engine = create_engine(f"sqlite:///{self.root}/t.db")
        from sqlalchemy import event

        @event.listens_for(self.engine, "connect")
        def _fast(dbapi_conn, _record):  # scratch DB: no fsync per commit
            dbapi_conn.execute("PRAGMA synchronous=OFF")
            dbapi_conn.execute("PRAGMA journal_mode=MEMORY")
        mdb.Base.metadata.create_all(self.engine)
        self.db = sessionmaker(bind=self.engine)()
        vod = self.vod

        def mapped(*parts):
            s = str(_RealPath(*parts))
            return _RealPath(str(vod) + s[len("/media/vod"):]) if s.startswith("/media/vod") else _RealPath(*parts)
        self._saved = {k: getattr(sync, k) for k in
                       ("Path", "TMDBService", "make_provider_client", "VOD_MOVIES_ROOT", "VOD_SERIES_ROOT")}
        sync.Path = mapped
        sync.TMDBService = _TMDB
        sync.VOD_MOVIES_ROOT = vod / "movies"
        sync.VOD_SERIES_ROOT = vod / "shows"
        _TMDB.world = self.world
        if not self.db.query(Provider).count():
            self._setup()

    def close(self):
        for k, v in self._saved.items():
            setattr(sync, k, v)
        self.db.close()
        self.engine.dispose()

    def _setup(self):
        w = self.world
        for p, prov in enumerate(w["providers"]):
            row = Provider(name=f"P{p}", server_url=f"http://{host_base(w, p)}", username=user_of(w, p),
                           password="p", active=True,
                           priority=p + 1, require_tmdb_match=prov["require_tmdb"])
            self.db.add(row)
            self.db.commit()
            for c in "ab":
                self.db.add(ProviderCategory(provider_id=row.id, category_id=c, category_name=f"{p}{c}",
                                             type="movie", whitelisted=True, source_tag=f"P{p}{c}"))
        for tid, how in w["radarr"].items():
            t, y = w["films"][tid]
            folder = sync.make_folder_name(t, y)
            if len(f"{folder} Bluray.mkv".encode()) > 250:
                how = "elsewhere"  # Radarr could not have made that folder either
            if how == "elsewhere":
                rp = f"/data/elsewhere/{folder}/{folder}.mkv"
            else:
                d = self.vod / "movies" / folder
                d.mkdir(parents=True, exist_ok=True)
                (d / f"{folder} Bluray.mkv").write_bytes(b"v")
                if how == "merged-nfo":
                    (d / "movie.nfo").write_text(f"<movie><tmdbid>{tid}</tmdbid></movie>")
                rp = f"/data/movies/{folder}/{folder} Bluray.mkv"
            if not self.db.query(Movie).filter_by(tmdb_id=tid).first():
                self.db.add(Movie(tmdb_id=tid, title=t, year=y, source="radarr", radarr_path=rp))
        for tid in w["unmanaged"]:
            t, y = w["films"][tid]
            folder = sync.vod_folder_name(t, y)
            if len(folder.encode()) < 240:
                d = self.vod / "movies" / folder
                d.mkdir(parents=True, exist_ok=True)
                (d / "home video.mkv").write_bytes(b"u")
        self.db.commit()

    def night(self, n):
        _TMDB.night = n
        _TMDB.calls = {"search": 0, "details": 0, "failed": 0}
        providers = self.db.query(Provider).order_by(Provider.id).all()
        hosts = {}
        for i, p in enumerate(providers):
            hosts[p.id] = host_of(self.world, i, n)
            if p.server_url != f"http://{hosts[p.id]}":
                p.server_url = f"http://{hosts[p.id]}"  # the admin changed the provider's host
        self.db.commit()
        clients = {p.id: _Client(self.world, i, hosts[p.id], n) for i, p in enumerate(providers)}
        sync.make_provider_client = lambda p: clients[p.id]
        statuses = []
        crash = self.world["fault"] == "crash" and n == 1
        calls = [0]

        def kill():
            calls[0] += 1
            if calls[0] > self.world["crash_after"]:
                raise _Killed()
            return False
        for p in providers:
            try:
                run = sync.sync_provider(p, "full", self.db, cancel_check=kill if crash else None)
                statuses.append((run.status, run.error_message))
            except _Killed:  # a process kill: nothing after it runs this night
                self.db.rollback()
                statuses.append(("killed", None))
                self.db.expire_all()
                return statuses, dict(_TMDB.calls)
            except Exception as e:  # S3: must never happen
                self.db.rollback()
                statuses.append(("raised", repr(e)))
        try:
            sync.sweep_orphaned_vod_records(self.db)
        except Exception as e:
            statuses.append(("raised", "sweep: " + repr(e)))
        self.db.expire_all()
        return statuses, dict(_TMDB.calls)

    def drop_namesake_strms(self):
        """Delete the .strm of every row that shares its title and year with
        another row (E25). Returns their paths relative to the VOD root."""
        rows = [m for m in self.db.query(Movie).all() if m.strm_path and m.provider_id]
        count = {}
        for m in rows:
            count[(m.title, m.year)] = count.get((m.title, m.year), 0) + 1
        out = set()
        for m in rows:
            if count[(m.title, m.year)] > 1:
                f = _RealPath(m.strm_path)
                if f.exists():
                    f.unlink()
                    out.add(str(f.relative_to(self.vod)))
        return out

    def snap(self):
        files = {}
        for f in sorted(self.vod.rglob("*")):
            rel = str(f.relative_to(self.vod))
            files[rel] = hashlib.md5(f.read_bytes()).hexdigest() if f.is_file() else "dir"
        rows = {}
        for m in self.db.query(Movie).all():
            strm = m.strm_path and str(_RealPath(m.strm_path).relative_to(self.vod)) if m.strm_path else None
            plays = None
            if m.strm_path:
                try:
                    plays = _RealPath(m.strm_path).read_text().strip()
                except OSError:
                    plays = None
            rows[str(m.tmdb_id)] = {"strm": strm, "src": m.source, "plays": plays,
                                    "radarr": m.radarr_path, "pid": m.provider_id}
        return {"files": files, "rows": rows}


def folder_films(snap, vod):
    """folder name -> set of films whose files are in it."""
    out = {}
    for tid, r in snap["rows"].items():
        if r["strm"]:
            out.setdefault(_RealPath(r["strm"]).parent.name, set()).add(tid)
        if r["radarr"] and "/data/movies/" in r["radarr"]:
            out.setdefault(_RealPath(r["radarr"]).parent.name, set()).add(tid)
    for rel in snap["files"]:
        if rel.endswith(".nfo") and rel.startswith("movies/"):
            try:
                text = (_RealPath(vod) / rel).read_text()
            except OSError:
                continue
            for a, b in sync_nfo_ids(text):
                out.setdefault(_RealPath(rel).parent.name, set()).add(str(int(a or b)))
    return out


def sync_nfo_ids(text):
    import re
    return re.findall(r"<tmdbid>\s*(-?\d+)\s*</tmdbid>|<uniqueid[^>]*type=\"tmdb\"[^>]*>\s*(-?\d+)\s*<", text, re.I)


def check(world, pre, nights, statuses, calls_last, folders, dropped=(), failed=()):
    """Invariant violations as (class, text). `nights` = [snap night 1, 2, 3]."""
    bad = []
    # O1: a row changes owner only on a plain name match by the new owner
    for n, snap in enumerate(nights, 1):
        before_snap = nights[n - 2] if n > 1 else pre
        for tid, r in snap["rows"].items():
            b = before_snap["rows"].get(tid)
            if not b or not b["pid"] or not r["pid"] or b["pid"] == r["pid"] or int(tid) <= 0:
                continue
            q = r["pid"] - 1
            plain = [s for s in world["providers"][q]["streams"]
                     if world["search"].get((world["films"][s["tid"]][0].lower(), world["films"][s["tid"]][1]))
                     == int(tid)]
            if not plain:
                bad.append(("O1", f"night {n}: row {tid} taken over by provider {q} on a provider id alone"))
    films = world["films"]
    search = world["search"]
    idx0 = stream_index(world, 0)

    def ident(url, idx):
        m = idx.get(url)
        return (m[0], m[1]) if m else None

    fault_night = 1 if world["fault"] else None
    for n, st in enumerate(statuses, 1):
        for status, err in st:
            if status != "completed" and not (n == fault_night and status in ("killed", "failed", "partial")):
                bad.append(("S3", f"night {n}: sync {status}: {err}"))
    pre_ident = {}
    for tid, r in pre["rows"].items():
        if r["plays"] and ident(r["plays"], idx0):
            pre_ident.setdefault(ident(r["plays"], idx0), []).append(tid)
    # A row whose file the test deleted (E25) may be removed by the orphan sweep
    # when its provider offers no stream whose own id names it: nothing can
    # tell which stream is really its own (755ea67's sweep rule).
    lost_ok = set()
    before_drop = dict(pre["rows"], **nights[0]["rows"])
    shared_paths = {p for p, n in collections.Counter(r["strm"] for r in before_drop.values() if r["strm"]).items()
                    if n > 1}
    for tid, r in before_drop.items():
        if r["strm"] in dropped and r["strm"] in shared_paths:
            lost_ok.add(tid)  # a #155 legacy shared file is never rewritten (E18)
        elif r["strm"] in dropped and r["pid"]:
            q = r["pid"] - 1  # providers are created in order: id = index + 1
            prov = world["providers"][q]
            own = [s for s in prov["streams"]
                   if (int(tid) > 0 and sync._provider_tmdb_hint({"tmdb": s["hint"]}) == int(tid)
                       and films[s["tid"]] == films.get(int(tid)))
                   or (int(tid) < 0 and int(tid) == -(r["pid"] * sync.NEGATIVE_ID_BLOCK + s["sid"]
                                                      + (5_000_000 if prov["renumber"] else 0)))]
            if not own:
                lost_ok.add(tid)
    for n, snap in enumerate(nights, 1):
        idx = stream_index(world, n)
        for tid, r in pre["rows"].items():
            now = snap["rows"].get(tid)
            if tid in lost_ok:
                continue  # removed by the orphan sweep; a later re-import is a new item
            if now is None:
                bad.append(("S1", f"night {n}: night-0 row {tid} lost"))
            elif now["strm"] != r["strm"]:
                bad.append(("S1", f"night {n}: row {tid} moved {r['strm']} -> {now['strm']}"))
        swept = {str(_RealPath(before_drop[t]["strm"]).parent) for t in lost_ok if before_drop[t]["strm"]}
        gone = [f for f in pre["files"] if f not in snap["files"] and f not in dropped
                and f not in swept and str(_RealPath(f).parent) not in swept]
        if gone:
            bad.append(("S1", f"night {n}: night-0 files gone: {gone[:3]}"))
        for name, tids in folders[n].items():
            if len(tids) > 1 and tids != folders[0].get(name):
                bad.append(("S2", f"night {n}: folder {name[:50]!r} holds films {sorted(tids)}"))
        plays = {}
        for tid, r in snap["rows"].items():
            if r["plays"] and ident(r["plays"], idx):
                plays.setdefault(ident(r["plays"], idx), []).append(tid)
        for key, tids in plays.items():
            if len(tids) > 1 and sorted(tids) != sorted(pre_ident.get(key, [])):
                bad.append(("S5", f"night {n}: stream {key} plays in rows {sorted(tids)}"))
        # R1: every row plays the film its tmdb_id names
        for tid, r in snap["rows"].items():
            if int(tid) <= 0 or not r["plays"] or r["plays"] not in idx:
                continue
            p, i, true, hint = idx[r["plays"]]
            if true == int(tid):
                continue
            before = pre["rows"].get(tid)
            b_true = idx0.get(before["plays"], (0, 0, None, None))[2] if before and before["plays"] else None
            # R1a holds while the row's file exists; a file the test deleted
            # (E25) is restored under R1b's rule below.
            if b_true == int(tid) and r["strm"] not in dropped:
                bad.append(("R1a", f"night {n}: row {tid} played its own film, now plays film {true} "
                                   f"(provider {p} stream {i}, id {hint!r})"))
                continue
            if before and before["plays"] and ident(before["plays"], idx0) == (p, i):
                continue  # unchanged since before the upgrade (a 755ea67 name match)
            name_says = search.get((films[true][0].lower(), films[true][1]))
            own_id = sync._provider_tmdb_hint({"tmdb": hint})
            # ... and TMDB can confirm that id (not 404, not rate-limited): an id
            # nobody can check is no evidence, and the name decides (755ea67).
            _TMDB.night = n
            verifiable = true not in world["not_on_tmdb"] and not _TMDB()._down(true)
            names_its_namesake = own_id == true and films[true] == films.get(int(tid)) and verifiable
            if own_id == int(tid) or (name_says == int(tid) and not names_its_namesake):
                right = [1 for q, pr in enumerate(world["providers"]) for s in pr["streams"]
                         if s["tid"] == int(tid) and sync._provider_tmdb_hint({"tmdb": s["hint"]}) == int(tid)]
                if right:
                    bad.append(("R1c", f"night {n}: row {tid} plays mislabelled film {true} though a "
                                       f"correctly labelled stream of {tid} is offered"))
                continue
            bad.append(("R1b", f"night {n}: new/changed row {tid} plays film {true} "
                               f"(provider {p} stream {i}, id {hint!r}), which nothing labels {tid}"))
    # R2: a plain import next to an unmanaged video behaves as 755ea67 (plain folder)
    for tid in world["unmanaged"]:
        t, y = films[tid]
        folder = sync.vod_folder_name(t, y)
        if len(folder.encode()) >= 240 or search.get((t.lower(), y)) != tid:
            continue
        claimed = folders[-1].get(folder, set()) - {str(tid)}
        # R2's allowed difference: a Radarr row of another film in a folder of
        # that name (by name: merged or not, Tentacle cannot tell) takes it.
        claimed |= {str(o) for o in world["radarr"]
                    if o != tid and sync.make_folder_name(*films[o]) == sync.make_folder_name(t, y)}
        r = nights[-1]["rows"].get(str(tid))
        if r and r["strm"] and "[tmdbid-" in r["strm"] and not claimed and r["src"].startswith("provider_"):
            hints = {sync._provider_tmdb_hint({"tmdb": s["hint"]}) for pr in world["providers"]
                     for s in pr["streams"] if s["tid"] == tid}
            if hints <= {None, tid}:
                bad.append(("R2", f"plain import {tid} diverted to {r['strm']} by an unmanaged video"))
    # S4: the night after a clean night changes nothing. Not clean: night 1 with
    # an injected fault; a night on which a TMDB lookup failed (its claims wait
    # for a night that can decide them, E12); with deleted files (E25), the
    # nights up to the orphan sweep (nights 2 and 3).
    # Also not clean: a night with a #154 takeover (755ea67 merges the new
    # owner's tag one night later; not a decision of this change).
    def took_over(a):
        before_snap = nights[a - 2] if a > 1 else pre
        return any(before_snap["rows"].get(k, {}).get("pid") not in (None, r["pid"])
                   for k, r in nights[a - 1]["rows"].items())
    pairs = [(a, a + 1) for a in range(1, len(nights))
             if not (a == 1 and world["fault"]) and not (failed and failed[a - 1])
             and not (world.get("drop_strm") and a in (1, 2, 3)) and not took_over(a)]
    for a, b in pairs:
        prev, last = nights[a - 1], nights[b - 1]
        if lost_ok:  # an allowed orphan-sweep removal (above) is not a change of decision
            gone = {before_drop[t]["strm"] for t in lost_ok}
            gone_dirs = {str(_RealPath(g).parent) for g in gone if g}
            def trim(snap):
                return {"files": {k: v for k, v in snap["files"].items()
                                  if k not in gone and str(_RealPath(k).parent) not in gone_dirs
                                  and k not in gone_dirs},
                        "rows": {k: v for k, v in snap["rows"].items() if k not in lost_ok}}
            prev, last = trim(prev), trim(last)
        if last != prev:
            df = sorted(k for k in set(last["files"]) | set(prev["files"])
                        if last["files"].get(k) != prev["files"].get(k))
            dr = sorted(k for k in set(last["rows"]) | set(prev["rows"]) if last["rows"].get(k) != prev["rows"].get(k))
            bad.append(("S4", f"night {b} changed files {df[:3]} rows {dr[:3]}"))
            break
    n_streams = sum(len(p["streams"]) for p in world["providers"])
    if calls_last["search"] > n_streams or calls_last["details"] > n_streams:
        bad.append(("S6", f"last night TMDB calls {calls_last} for {n_streams} streams"))
    return bad


BLOCKING = {"S1", "S2", "S3", "S4", "S5", "S6", "R1a", "R1b", "R2", "O1"}


def run_seed(seed, root, night0=None, v2=True):
    """Night 0 (this code, provider ids not sent) unless `night0` already built
    the library in `root`; then nights 1-3. Night 1 may carry the injected
    fault; S4 compares nights 2 and 3. Returns (violations, info)."""
    w = World(seed, root, v2)
    try:
        if night0 is None:
            w.night(0)
        pre = w.snap()
        folders = [folder_films(pre, w.vod)]
        nights, statuses, calls = [], [], []
        dropped = set()
        for n in ((1, 2, 3, 4, 5) if w.world.get("drop_strm") else (1, 2, 3)):
            if n == 2 and w.world.get("drop_strm"):
                dropped = w.drop_namesake_strms()
            st, c = w.night(n)
            statuses.append(st)
            calls.append(c)
            nights.append(w.snap())
            folders.append(folder_films(nights[-1], w.vod))
        bad = check(w.world, pre, nights, statuses, calls[-1], folders, dropped,
                    [c["failed"] for c in calls])
        info = {"seed": seed, "rows0": len(pre["rows"]), "rows3": len(nights[-1]["rows"]),
                "calls3": calls[-1], "tmdb": w.world["tmdb_mode"], "fault": w.world["fault"],
                "modes": [p.get("mode", "mixed") for p in w.world["providers"]]}
        return bad, info
    finally:
        w.close()


def _seeds():
    first = int(os.environ.get("TENTACLE_FUZZ_FIRST", "0"))
    n = int(os.environ.get("TENTACLE_FUZZ_SEEDS", "12"))
    return list(range(first, first + n))


def _run(cases, classes):
    """Violations of `classes` over (v2, seed) cases, as strings."""
    import logging
    out = []
    prev = logging.root.manager.disable
    logging.disable(logging.CRITICAL)
    try:
        for v2, seed in cases:
            root = tempfile.mkdtemp()
            try:
                bad, _ = run_seed(seed, root, v2=v2)
            finally:
                shutil.rmtree(root, True)
            out += [f"seed {seed}{'' if v2 else ' (v1)'}: {c} {b}" for c, b in bad if c in classes]
    finally:
        logging.disable(prev)
    return out


# Classes the code at 0653661 violated (CONTRACT-185.md section 8), each with
# seeds that showed it; they now run as regression seeds for their class.
KNOWN = {
    "R1a": [(True, 205), (True, 208), (True, 224)],               # swapped ids: the heal crosses a right row
    "S5": [(True, 80), (True, 97), (True, 194)],                  # provider host change
    "S4": [(False, 1460), (False, 1850), (False, 2059), (False, 2907), (False, 5164),
           (True, 437), (True, 987), (True, 1014)],               # order dependence (review causes A and B)
    "R2": [(True, 6), (True, 7), (True, 8)],                      # a plain import diverted by an unmanaged video
    "O1": [(True, 364), (True, 528), (True, 580)],                # a claim takes a row over (E26)
    "R1b": [(True, 670), (True, 841), (True, 1178)],              # a missing .strm restored from the wrong namesake (E25)
}
HOLDING = BLOCKING


class NamesakePropertyTest(unittest.TestCase):
    def cases(self):
        known = [c for v in KNOWN.values() for c in v]
        return sorted(set([(False, s) for s in REGRESSION_SEEDS] + [(True, s) for s in _seeds()] + known))

    def test_invariants_hold_for_random_libraries(self):
        failures = _run(self.cases(), HOLDING)
        self.assertEqual(failures, [], "\n".join(failures[:30]))

    def _known(self, cls):
        cases = sorted(set(KNOWN[cls] + [(True, s) for s in _seeds()]))
        failures = _run(cases, {cls})
        self.assertEqual(failures, [], "\n".join(failures[:30]))

    def test_R1a_swapped_ids_never_make_a_right_row_wrong(self):
        self._known("R1a")

    def test_S5_host_change_keeps_one_row_per_stream(self):
        self._known("S5")

    def test_S4_decisions_do_not_depend_on_order(self):
        self._known("S4")

    def test_R2_plain_import_next_to_an_unmanaged_video_is_unchanged(self):
        self._known("R2")

    def test_O1_a_provider_id_never_takes_a_row_over(self):
        self._known("O1")

    def test_R1b_a_new_or_changed_row_plays_its_own_film(self):
        self._known("R1b")


if __name__ == "__main__":
    # Scratch runner:  python tests/test_vod_namesakes_property.py <root> <seed> [night0-only|nights]
    if len(sys.argv) > 2:
        import logging
        logging.disable(logging.CRITICAL)
        root, seed, mode = sys.argv[1], int(sys.argv[2]), (sys.argv[3] if len(sys.argv) > 3 else "all")
        if mode == "night0-only":
            w = World(seed, root)
            st0, _ = w.night(0)
            w.close()
            print(json.dumps({"night0": st0}))
        else:
            bad, info = run_seed(seed, root, night0=True if mode == "nights" else None)
            print(json.dumps({"bad": bad, "info": info}, default=str))
    else:
        unittest.main()
