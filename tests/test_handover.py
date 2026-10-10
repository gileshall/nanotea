"""After a listener hands its agent items and exits: busy while it works on them, away once that runs long."""

import unittest

from nanotea.server import RELISTEN_GRACE_S, after_handover


class Handover(unittest.TestCase):
    def test_busy_then_away(self):
        self.assertEqual(after_handover(100.0, False, 90.0, 100.0 + RELISTEN_GRACE_S - 1), "busy")
        self.assertEqual(after_handover(100.0, False, 90.0, 100.0 + RELISTEN_GRACE_S), "away")

    def test_a_listener_since_ends_it(self):
        self.assertIsNone(after_handover(100.0, False, 101.0, 100.0 + RELISTEN_GRACE_S * 10))
        self.assertIsNone(after_handover(100.0, True, 90.0, 100.0 + RELISTEN_GRACE_S * 10))

    def test_never_handed(self):
        self.assertIsNone(after_handover(None, False, 0.0, 1e9))


if __name__ == "__main__":
    unittest.main()
