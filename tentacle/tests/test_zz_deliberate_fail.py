import unittest


class T(unittest.TestCase):
    def test_fails(self):
        self.assertEqual(1, 2)
