"""Unit tests proving the test harness itself is isolated, network-guarded, and secret-free."""
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

import requests

from tests.harness import (
    IsolatedTestCase,
    NetworkBlockedInTestsError,
    NetworkGuard,
    TEST_MASTER_API_KEY,
    copy_sqlite_readonly,
    create_sanitized_snapshot,
    get_isolation_root,
    reset_test_db,
)
from tests.harness.fixtures import make_dirty_test_db
from tests.harness.snapshot import TEST_MASTER_API_KEY as _SNAP_MASTER_KEY
import database


class TestSecurityAndHarness(IsolatedTestCase):
    def test_01_network_guard_blocks_unmocked_outbound_http_immediately(self):
        """NetworkGuard raises NetworkBlockedInTestsError in 0ms on unmocked requests calls."""
        self.assertTrue(NetworkGuard.is_active())
        with self.assertRaises(NetworkBlockedInTestsError):
            requests.get("http://audiobookshelf:80/api/ping", timeout=10)

        with self.assertRaises(NetworkBlockedInTestsError):
            requests.post("http://127.0.0.1:13378/api/login", json={}, timeout=10)

    def test_02_network_guard_permits_mocked_requests_and_allow_live_context(self):
        """Mocked HTTP calls work normally and NetworkGuard.allow_live() scopes strictly."""
        with patch("requests.get") as mock_get:
            mock_get.return_value.status_code = 200
            resp = requests.get("https://example.invalid/test")
            self.assertEqual(resp.status_code, 200)
            mock_get.assert_called_once()

        with NetworkGuard.allow_live():
            self.assertFalse(NetworkGuard.is_active())
        self.assertTrue(NetworkGuard.is_active())

    def test_03_copy_readonly_and_sanitize_preserves_source_and_scrubs_all_secrets(self):
        """Snapshot copy+sanitize never touches source bytes/mtime and wipes every secret & PII."""
        with tempfile.TemporaryDirectory() as tmpdir:
            src_db = Path(tmpdir) / "source_fake_live.db"
            make_dirty_test_db(src_db)
            src_bytes_before = src_db.read_bytes()
            src_sha_before = hashlib.sha256(src_bytes_before).hexdigest()
            src_mtime_before = src_db.stat().st_mtime_ns

            snap_db = get_isolation_root() / "sanitized_dirty_check.db"
            out_path, stats = create_sanitized_snapshot(src=src_db, dest_path=snap_db)
            self.assertEqual(out_path, snap_db)
            self.assertGreaterEqual(stats["system_settings_scrubbed"], 5)
            self.assertEqual(stats["admin_users_scrubbed"], 1)
            self.assertEqual(stats["profiles_scrubbed"], 1)
            self.assertEqual(stats["devices_scrubbed"], 1)

            # Verify source file remained 100% immutable
            self.assertEqual(hashlib.sha256(src_db.read_bytes()).hexdigest(), src_sha_before)
            self.assertEqual(src_db.stat().st_mtime_ns, src_mtime_before)

            # Inspect scrubbed snapshot directly
            conn = sqlite3.connect(str(snap_db))
            conn.row_factory = sqlite3.Row
            try:
                settings = {r["key"]: json.loads(r["value"]) for r in conn.execute("SELECT key, value FROM system_settings").fetchall()}
                self.assertEqual(settings["master_api_key"], _SNAP_MASTER_KEY)
                self.assertEqual(settings["abs_base_url"], "http://abs.test.invalid")
                self.assertEqual(settings["abs_api_key"], "")
                self.assertEqual(settings["yoto_refresh_token"], "")
                self.assertEqual(settings["yoto_client_id"], "")
                self.assertEqual(settings["openrouter_api_key"], "")
                sso = settings["sso_config"]
                self.assertEqual(sso["oidc_client_secret"], "")
                self.assertEqual(sso["extra_trusted_proxies"], [])

                u = dict(conn.execute("SELECT * FROM admin_users LIMIT 1").fetchone())
                self.assertEqual(u["username"], "test_user_1")
                self.assertEqual(u["email"], "user_1@example.test")
                self.assertEqual(u["abs_api_key"], "")
                self.assertEqual(u["sso_subject"], "")
                self.assertNotIn("real_parent_name", json.dumps(u))

                p = dict(conn.execute("SELECT * FROM profiles WHERE id = 'secret_kid'").fetchone())
                self.assertEqual(p["abs_api_key"], "")
                self.assertEqual(p["abs_username"], "")
                self.assertEqual(p["display_name"], "Profile 1")

                d = dict(conn.execute("SELECT * FROM esp32_devices LIMIT 1").fetchone())
                self.assertEqual(d["ip_address"], "198.51.100.1")
                self.assertEqual(d["parent_pin"], "1234")
                self.assertNotEqual(d["friendly_name"], "Kid Bedroom Frame")

                # Media structure kept while query secrets removed
                m = dict(conn.execute("SELECT * FROM media_items WHERE id = 'dirty_book_1'").fetchone())
                self.assertEqual(m["title"], "Clean Title For Snapshot Test")
                self.assertNotIn("SECRET_TOK_123", m["cover_url"])
                self.assertNotIn("ABS_KEY_9", m["raw_metadata"])

                # Bridge track host normalized while track retained
                tr = dict(conn.execute("SELECT * FROM bridge_tracks WHERE slot_id = 'dirty_slot_1'").fetchone())
                self.assertEqual(tr["stream_url"], "http://storydeck.test.invalid/s/audio/dirty_slot_1/1")
            finally:
                conn.close()

    def test_04_reset_test_db_isolates_per_call(self):
        """reset_test_db() rotates onto a clean schema-v17 SQLite file with cleared data."""
        database.upsert_profile("ephemeral_p1", {"display_name": "Ephemeral 1"})
        self.assertIsNotNone(database.get_profile_by_id("ephemeral_p1"))
        reset_test_db()
        self.assertIsNone(database.get_profile_by_id("ephemeral_p1"))
        self.assertFalse(str(database.DB_PATH).endswith("broker.db"))


if __name__ == "__main__":
    unittest.main()
