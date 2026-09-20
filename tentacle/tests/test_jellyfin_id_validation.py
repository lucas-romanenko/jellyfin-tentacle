"""Jellyfin item ids must never be able to re-point a request at another endpoint.

Run from the tentacle/ directory:  python -m unittest discover -s tests
"""
import unittest

import requests


class JellyfinItemIdTests(unittest.TestCase):
    def test_requests_resolves_dot_segments_client_side(self):
        """Why the validation is needed, pinned as an executable fact.

        `requests` normalizes `..` before sending, so an unvalidated id escapes
        the /Items/ prefix entirely — and the session carries the Jellyfin admin
        API key.
        """
        prepared = requests.Request(
            "DELETE", "http://jellyfin.example:8096/Items/../Users/" + "a" * 32
        ).prepare()
        self.assertEqual(prepared.url,
                         "http://jellyfin.example:8096/Users/" + "a" * 32)

    def test_traversal_ids_are_rejected(self):
        from fastapi import HTTPException
        from routers.library import _validate_jellyfin_item_id
        for bad in ("../Users/" + "a" * 32, "..%2fUsers", "a" * 31, "", None,
                    "x" * 32, "../../System/Restart"):
            with self.assertRaises(HTTPException, msg=f"accepted {bad!r}") as ctx:
                _validate_jellyfin_item_id(bad)
            self.assertEqual(ctx.exception.status_code, 400)

    def test_real_jellyfin_ids_are_accepted(self):
        from routers.library import _validate_jellyfin_item_id
        for good in ("0123456789abcdef0123456789ABCDEF",
                     "12345678-1234-1234-1234-123456789abc"):
            self.assertEqual(_validate_jellyfin_item_id(good), good)


if __name__ == "__main__":
    unittest.main()
