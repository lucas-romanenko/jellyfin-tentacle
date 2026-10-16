"""Property test for #627: remakes of one title in the library,
random relabels onto each other's years, re-lists, second listings, hints,
delistings, failed fetches. Invariants after every night:
 R1 a film whose .strm plays a stream that is listed tonight with the film's
    title, and that no other of our films plays, and that carries no hint naming
    another film, is still in the library after the night (never pruned);
 R2 no two films' .strm play the same stream;
 R3 a film's .strm never changes to a stream another of our films played before
    the night (no stealing).
REMAKES_PROPERTY_SEEDS random seeds (default 25; 1,000 in review), from
REMAKES_PROPERTY_START. Run from tentacle/: python -m unittest tests.test_vod_relabel_remakes_property
"""
import os, random, unittest
from pathlib import Path as _P
import services.sync as sync
from models.database import Movie
from test_vod_namesakes import TMDB, stream
from test_vod_relisted_streams import Relisted, sync_meta, FILLER
from test_vod_relabel_onto_own_remake import YearAwareTMDB

SEEDS = int(os.environ.get("REMAKES_PROPERTY_SEEDS", "25"))
START = int(os.environ.get("REMAKES_PROPERTY_START", "0"))
TITLES = ["Heat", "Dune", "Psycho"]


class RemakesProperty(Relisted):
    def setUp(self):
        super().setUp()
        sync.TMDBService = YearAwareTMDB

    def plays(self):
        out = {}
        for m in self.db.query(Movie).all():
            p = _P(m.strm_path) if m.strm_path else None
            if p and p.exists():
                t = p.read_text().strip()
                if t:
                    out[m.tmdb_id] = int(t.rsplit("/", 1)[-1].split(".")[0])
        return out

    def run_seed(self, seed):
        rnd = random.Random(seed)
        self.tearDown(); self.setUp()
        TMDB.films, TMDB.search = {}, {}
        for n, sid in FILLER:
            TMDB.films[sid] = (n, "2010")
        films = {}
        tid = 100
        for t in rnd.sample(TITLES, rnd.randint(1, 2)):
            years = sorted(rnd.sample(range(1960, 2024), rnd.randint(2, 3)))
            for y in years:
                TMDB.films[tid] = (t, str(y))
                films[tid] = {"title": t, "year": y, "label": y, "sid": 1000 + tid * 10, "hint": False,
                              "listed": True, "extra": None}
                tid += 1
        bad = []
        for night in range(5):
            if night:
                for f_id, f in films.items():
                    ev = rnd.random()
                    sib = [o for o in films.values() if o["title"] == f["title"] and o is not f]
                    if ev < 0.25 and sib:
                        f["label"] = rnd.choice(sib)["year"]        # relabel onto a remake's year
                    elif ev < 0.35:
                        f["label"] = f["year"]                      # label corrected
                    elif ev < 0.45:
                        f["sid"] += 1                               # re-listed under a new id
                    elif ev < 0.5:
                        f["listed"] = not f["listed"]
                    elif ev < 0.55:
                        f["extra"] = None if f["extra"] else f["sid"] + 5
                    elif ev < 0.6:
                        f["hint"] = not f["hint"]
            listing = []
            for f_id, f in films.items():
                if f["listed"]:
                    listing.append(stream(f"{f['title']} ({f['label']})", f["sid"], f_id if f["hint"] else None))
                    if f["extra"]:
                        listing.append(stream(f"{f['title']} ({f['year']})", f["extra"], f_id if f["hint"] else None))
            self.client.movies = {"a": listing + [stream(f"{n} (2010)", s) for n, s in FILLER]}
            fetch_fail = rnd.random() < 0.1
            self.client.raise_for = {"b"} if fetch_fail else set()
            before = self.plays()
            self.night()
            self.db.expire_all()
            after = self.plays()
            listed = {s["stream_id"]: s for s in listing}
            # R2
            seen = {}
            for f_id, s in after.items():
                if s in seen and f_id in films and seen[s] in films:
                    bad.append(f"seed {seed} night {night}: R2 films {seen[s]} and {f_id} both play {s}")
                seen[s] = f_id
            # R3
            for f_id, s in after.items():
                if f_id in films and before.get(f_id) != s and s in before.values():
                    other = [o for o, v in before.items() if v == s and o != f_id]
                    if other:
                        bad.append(f"seed {seed} night {night}: R3 film {f_id} took stream {s} from {other}")
            # R1
            for f_id, s in before.items():
                if f_id not in films or s not in listed or fetch_fail:
                    continue
                st = listed[s]
                hint = st.get("tmdb")
                if hint not in (None, "", f_id) and str(hint) != str(f_id):
                    continue
                if sum(1 for v in before.values() if v == s) != 1:
                    continue
                if not st["name"].startswith(films[f_id]["title"] + " ("):
                    continue
                if f_id not in after:
                    bad.append(f"seed {seed} night {night}: R1 film {f_id} {films[f_id]['title']} "
                               f"({films[f_id]['year']}) lost although it played listed stream {s} '{st['name']}'")
        return bad

    def test_property(self):
        import logging
        logging.disable(logging.CRITICAL)
        self.addCleanup(logging.disable, logging.NOTSET)
        allb = []
        for seed in range(START, START + SEEDS):
            allb += self.run_seed(seed)
        kinds = {}
        for b in allb:
            kinds.setdefault(b.split(": ", 1)[1].split(" ")[0], []).append(b)
        self.assertEqual([], allb, f"{len(allb)} violations over {SEEDS} seeds from {START}: " +
                         "; ".join(f"{k} x{len(v)}, e.g. {v[0]}" for k, v in sorted(kinds.items())))


if __name__ == "__main__":
    unittest.main()
