"""Opt-in live homelab integration tests for Audiobookshelf profile scoping.

Never runs in default unit suites or CI. Execute explicitly via:
    python scripts/run_tests.py --live
or:
    STORYDECK_LIVE_ABS_TESTS=1 python -m unittest tests.live.test_live_abs_scoping -v
"""
import os
import unittest

from tests.harness import NetworkGuard
import tests.test_isolation  # noqa: F401 - ensures isolation env before any DB import


@unittest.skipUnless(
    os.getenv("STORYDECK_LIVE_ABS_TESTS") == "1",
    "Opt-in live homelab ABS integration test (set STORYDECK_LIVE_ABS_TESTS=1)",
)
class TestLiveAbsProfileScoping(unittest.TestCase):
    def setUp(self):
        import database
        database.init_db()
        import clients
        self.abs_client = clients.abs_client
        self.assertTrue(self.abs_client and self.abs_client.base_url, "ABS Client must be initialized")

    def test_auto_resolve_user_tokens(self):
        """Tests that user tokens can be resolved automatically using username."""
        with NetworkGuard.allow_live():
            ca_token = self.abs_client.get_user_token("child_a")
            cb_token = self.abs_client.get_user_token("child_b")
        self.assertIsNotNone(ca_token)
        self.assertIsNotNone(cb_token)
        self.assertGreater(len(ca_token), 20)

    def test_child_profile_scoped_catalog(self):
        """Tests that get_items_for_profile() returns restricted child list without explicit api_key."""
        with NetworkGuard.allow_live():
            master_books = self.abs_client.get_items_for_profile({})
            ca_books = self.abs_client.get_items_for_profile({"abs_username": "child_a"})
            cb_books = self.abs_client.get_items_for_profile({"abs_username": "child_b"})

        self.assertGreaterEqual(len(master_books), 100)
        self.assertLess(len(ca_books), len(master_books))
        self.assertGreater(len(ca_books), 0)
        self.assertLess(len(cb_books), len(master_books))
        self.assertGreater(len(cb_books), 0)


if __name__ == "__main__":
    unittest.main()
