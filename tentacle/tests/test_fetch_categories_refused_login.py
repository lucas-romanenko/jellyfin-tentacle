"""Fetch categories when the Xtream panel refuses the login.

Run from the tentacle/ directory:  python -m unittest discover -s tests

A wrong or expired account gets {"user_info": {"auth": 0}} for every action.
fetch_categories walked that object as the category list and failed on its
keys: HTTP 500 "Internal server error" on the VOD page, and an empty category
list with no message in the categories dialog. It must be a 400 that says the
login was refused, and leave the stored categories alone.
"""
import json
import unittest
from unittest import mock

from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from models.database import Base
from tmp_dirs import temp_dir
from models.database import Provider, ProviderCategory
from routers import providers as providers_router

REFUSED = {"user_info": {"auth": 0}}


def fresh_db(test):
    engine = create_engine(f"sqlite:///{temp_dir(test)}/t.db")
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    test.addCleanup(db.close)
    return db


class _Sess:
    def __init__(self, body):
        self.body = body
        self.headers = {}

    def get(self, url, **kw):
        r = mock.Mock()
        r.status_code = 200
        r.json.return_value = json.loads(json.dumps(self.body))
        return r


class FetchCategoriesRefused(unittest.TestCase):
    def setUp(self):
        self.db = fresh_db(self)
        p = Provider(name="p", server_url="http://prov.test", username="u", password="p", active=True)
        self.db.add(p); self.db.commit(); self.pid = p.id
        self.db.add(ProviderCategory(provider_id=self.pid, category_id="1", category_name="Kept",
                                     type="movie", whitelisted=True, title_count=7))
        self.db.commit()

    def run_fetch(self, body):
        with mock.patch.object(providers_router.requests, "Session", return_value=_Sess(body)), \
             mock.patch("services.provider_activity.refuse_while_recording", return_value=None):
            return providers_router.fetch_categories(self.pid, self.db)

    def test_refused_login_is_a_400_and_changes_nothing(self):
        try:
            self.run_fetch(REFUSED)
            self.fail("a refused login was accepted as a category list")
        except HTTPException as e:
            self.assertEqual(e.status_code, 400)
            self.assertIn("refused", e.detail.lower())
        except TypeError as e:
            self.fail(f"500 (unhandled {type(e).__name__}: {e})")
        cats = self.db.query(ProviderCategory).filter(ProviderCategory.provider_id == self.pid).all()
        self.assertEqual([(c.category_name, c.title_count, c.whitelisted) for c in cats], [("Kept", 7, True)])

    def test_other_json_object_is_a_400(self):
        try:
            self.run_fetch({"error": "maintenance"})
            self.fail("a JSON object was accepted as a category list")
        except HTTPException as e:
            self.assertEqual(e.status_code, 400)
        except (TypeError, KeyError) as e:
            self.fail(f"500 (unhandled {type(e).__name__}: {e})")

    def test_normal_list_still_works(self):
        out = self.run_fetch([{"category_id": "2", "category_name": "New"}])
        self.assertTrue(out["success"])
        self.assertEqual(out["new_categories"], 2)   # once as a movie, once as a series category

    def test_empty_answer_is_no_categories(self):
        for body in ([], {}, None):
            out = self.run_fetch(body)
            self.assertEqual(out["new_categories"], 0, repr(body))


if __name__ == "__main__":
    unittest.main()
