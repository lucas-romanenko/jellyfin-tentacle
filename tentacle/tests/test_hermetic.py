"""The network guard (tests/hermetic.py) holds for the whole run."""
import socket
import unittest

import hermetic
from services.ssrf import is_safe_url


@unittest.skipUnless(socket.getaddrinfo.__name__ == "_getaddrinfo",
                     "run through scripts/check (tests/hermetic.py installs the guard)")
class TestNoNetwork(unittest.TestCase):
    def test_a_real_name_does_not_resolve(self):
        with self.assertRaises(socket.gaierror):
            socket.getaddrinfo("api.themoviedb.org", 443)

    def test_nothing_connects_off_the_machine(self):
        with socket.socket() as s, self.assertRaises(ConnectionRefusedError):
            s.connect(("104.16.0.1", 443))

    def test_loopback_still_works(self):
        with socket.socket() as srv:
            srv.bind(("127.0.0.1", 0))
            srv.listen(1)
            with socket.create_connection(srv.getsockname(), timeout=5):
                pass

    def test_trakt_passes_the_public_address_check(self):
        self.assertTrue(is_safe_url("https://api.trakt.tv/users/u/lists/l/items", allowed_hosts={"api.trakt.tv"}))
        self.assertIn("api.trakt.tv", hermetic.FAKE_DNS)


if __name__ == "__main__":
    unittest.main()
