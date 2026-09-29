"""A guide refresh leaves exactly one Tentacle XMLTV listing provider (#274).

Run from the tentacle/ directory:  python -m unittest discover -s tests

refresh_jellyfin_guide re-creates Tentacle's listing provider in Jellyfin and
deletes the old one. Two refreshes at once (two dashboard tabs, a YouTube run
finishing with a sync) both read [L1], both created a copy and both deleted
L1: two were left. Jellyfin 10.11 can also save the provider and THEN answer
500 (ListingsManager.SaveListingProvider saves the config before queueing
RefreshGuide, which can throw), and the old one was never deleted. Every
later refresh re-created each copy, so the count only grew.

The fake models Jellyfin: the provider is saved first, then the reply may be
a 500.
"""
import json
import threading
import time
import unittest
import uuid
from unittest import mock

import requests

TENTACLE = "http://tentacle:8888/api/live/xmltv.xml"
OTHER = "http://guides.example/epg.xml"


class FakeJellyfin:
    def __init__(self, providers=None, fail_next_post_after_save=False, refuse_post=False, slow=0.0):
        self.providers = providers if providers is not None else [
            {"Id": "orig", "Type": "xmltv", "Path": TENTACLE, "ChannelMappings": [{"Name": "a", "Value": "b"}]}]
        self.fail = fail_next_post_after_save
        self.refuse = refuse_post
        self.slow = slow
        self.lock = threading.Lock()
        self.guide_runs = 0

    def _resp(self, code, js=None):
        r = requests.Response()
        r.status_code = code
        r._content = (json.dumps(js) if js is not None else "").encode()
        r.url = "http://jf/x"
        return r

    def get(self, url, headers=None, timeout=None, params=None):
        if url.endswith("/System/Configuration/livetv"):
            with self.lock:
                snap = [dict(p) for p in self.providers]
            time.sleep(self.slow)
            return self._resp(200, {"ListingProviders": snap})
        if url.endswith("/ScheduledTasks"):
            return self._resp(200, [{"Key": "RefreshGuide", "Id": "t1"}])
        raise AssertionError(url)

    def post(self, url, headers=None, json=None, timeout=None):
        if url.endswith("/LiveTv/ListingProviders"):
            if self.refuse:
                return self._resp(400)
            with self.lock:
                new = dict(json, Id=uuid.uuid4().hex)
                self.providers.append(new)          # SaveConfiguration happens first
                if self.fail:                       # ...then CancelIfRunningAndQueue throws
                    self.fail = False
                    return self._resp(500)
            return self._resp(200, new)
        if "/ScheduledTasks/Running/" in url:
            self.guide_runs += 1
            return self._resp(204)
        raise AssertionError(url)

    def delete(self, url, headers=None, timeout=None, params=None):
        pid = (params or {}).get("Id") or url.split("Id=")[1]
        with self.lock:
            self.providers = [p for p in self.providers if p["Id"] != pid]
        return self._resp(204)

    def tentacle(self):
        return [p for p in self.providers if p["Path"] == TENTACLE]


def _patch(fake):
    import services.jellyfin_guide as g
    return mock.patch.multiple(g.requests, get=fake.get, post=fake.post, delete=fake.delete)


class OneProviderAfterARefresh(unittest.TestCase):
    def test_sequential_refreshes_keep_one(self):
        from services.jellyfin_guide import refresh_jellyfin_guide
        fake = FakeJellyfin()
        with _patch(fake):
            for _ in range(3):
                refresh_jellyfin_guide("http://jf", "k")
        self.assertEqual(1, len(fake.providers))
        self.assertNotEqual("orig", fake.providers[0]["Id"])          # re-created
        self.assertEqual([{"Name": "a", "Value": "b"}], fake.providers[0]["ChannelMappings"])
        self.assertEqual(3, fake.guide_runs)

    def test_jellyfin_500_after_save_leaves_no_duplicate(self):
        from services.jellyfin_guide import refresh_jellyfin_guide
        fake = FakeJellyfin(fail_next_post_after_save=True)
        with _patch(fake):
            refresh_jellyfin_guide("http://jf", "k")      # saved: that is a success
            self.assertEqual(1, len(fake.providers))
            refresh_jellyfin_guide("http://jf", "k")
        self.assertEqual(1, len(fake.providers), f"{len(fake.providers)} Tentacle listing providers left")

    def test_two_concurrent_refreshes_leave_one(self):
        from services.jellyfin_guide import refresh_jellyfin_guide
        fake = FakeJellyfin(slow=0.05)   # both read the config before either deletes
        with _patch(fake):
            ts = [threading.Thread(target=refresh_jellyfin_guide, args=("http://jf", "k")) for _ in range(2)]
            [t.start() for t in ts]
            [t.join() for t in ts]
            self.assertEqual(1, len(fake.providers))
            refresh_jellyfin_guide("http://jf", "k")
        self.assertEqual(1, len(fake.providers), f"{len(fake.providers)} Tentacle listing providers left")

    def test_existing_duplicates_collapse_to_one(self):
        from services.jellyfin_guide import refresh_jellyfin_guide
        fake = FakeJellyfin(providers=[{"Id": f"d{i}", "Type": "xmltv", "Path": TENTACLE} for i in range(13)])
        with _patch(fake):
            refresh_jellyfin_guide("http://jf", "k")
        self.assertEqual(1, len(fake.providers))

    def test_a_refused_post_keeps_the_old_provider_and_raises(self):
        from services.jellyfin_guide import refresh_jellyfin_guide
        fake = FakeJellyfin(refuse_post=True)
        with _patch(fake), self.assertRaises(requests.HTTPError):
            refresh_jellyfin_guide("http://jf", "k")
        self.assertEqual(["orig"], [p["Id"] for p in fake.providers])

    def test_other_xmltv_providers_are_never_touched(self):
        from services.jellyfin_guide import refresh_jellyfin_guide
        fake = FakeJellyfin(providers=[
            {"Id": "o1", "Type": "xmltv", "Path": OTHER},
            {"Id": "o2", "Type": "xmltv", "Path": OTHER},
            {"Id": "sd", "Type": "SchedulesDirect", "Path": None},
            {"Id": "t1", "Type": "xmltv", "Path": TENTACLE}])
        with _patch(fake):
            refresh_jellyfin_guide("http://jf", "k")
        ids = [p["Id"] for p in fake.providers]
        self.assertEqual(["o1", "o2", "sd"], ids[:3])
        self.assertEqual(1, len(fake.tentacle()))


if __name__ == "__main__":
    unittest.main()
