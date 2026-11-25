"""
Unit & integration tests for StoryDeck Touch Upstream Release Service & Admin Endpoints.
Tests:
  - Firmware asset matching (firmware.bin, storydeck-touch-*-firmware.bin)
  - Stable priority vs. pre-release opt-in filtering
  - Strict absence of automatic background downloads (notification-only)
  - User-initiated on-demand download, 0xE9 ESP-IDF image magic verification, and SHA-256 cataloging
  - Flask admin endpoints for upstream releases, preferences, and download triggers
"""

import tests.test_isolation  # noqa: F401 - must precede app/core imports
import hashlib
import io
import os
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from app import app
import core.config as core_config
import database
from core import touch_release_service


class TestTouchReleaseService(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        database.init_db()

    def setUp(self):
        self.client = app.test_client()
        touch_release_service.invalidate_touch_release_cache()

        # Clean up database tables
        conn = database.get_db_connection()
        try:
            with conn:
                conn.execute("DELETE FROM firmware_releases;")
                conn.execute("DELETE FROM esp32_devices WHERE mac_address LIKE 'test:touch:%';")
                conn.execute("DELETE FROM system_settings WHERE key = 'touch_include_prereleases';")
        finally:
            conn.close()

        # Clean up FIRMWARE_DIR test binaries
        fw_dir = Path(core_config.FIRMWARE_DIR)
        if fw_dir.exists():
            for f in fw_dir.glob("firmware_test_*.bin"):
                f.unlink(missing_ok=True)
            for f in fw_dir.glob("firmware_v*.bin"):
                f.unlink(missing_ok=True)

    def tearDown(self):
        touch_release_service.invalidate_touch_release_cache()

    def _login_admin(self):
        with self.client.session_transaction() as sess:
            sess["user_id"] = "admin_user"
            sess["username"] = "admin"
            sess["role"] = "admin"

    def _make_esp32_bin(self, tag: bytes = b"TOUCH_UPSTREAM_TEST") -> bytes:
        # Starts with 0xE9 ESP-IDF image magic byte and >= 24 bytes
        return b"\xe9\x04\x02\x20" + tag + (b"\xbb" * 64)

    def test_01_find_firmware_asset(self):
        """Verifies accurate identification of the firmware application binary asset."""
        assets = [
            {"name": "storydeck-touch-v0.1.0-bootloader.bin", "size": 16384, "browser_download_url": "http://example.com/bootloader.bin"},
            {"name": "storydeck-touch-v0.1.0-partitions.bin", "size": 4096, "browser_download_url": "http://example.com/partitions.bin"},
            {"name": "storydeck-touch-v0.1.0-firmware.bin", "size": 1843200, "browser_download_url": "http://example.com/fw.bin"},
            {"name": "storydeck-touch-v0.1.0-factory.bin", "size": 2097152, "browser_download_url": "http://example.com/factory.bin"},
        ]
        asset = touch_release_service.find_firmware_asset(assets)
        self.assertIsNotNone(asset)
        self.assertEqual(asset["name"], "storydeck-touch-v0.1.0-firmware.bin")

        # Tests simple 'firmware.bin'
        simple_assets = [
            {"name": "bootloader.bin", "size": 16384},
            {"name": "firmware.bin", "size": 1500000},
        ]
        self.assertEqual(touch_release_service.find_firmware_asset(simple_assets)["name"], "firmware.bin")

        # Tests no firmware asset
        irrelevant_assets = [
            {"name": "bootloader.bin", "size": 16384},
            {"name": "README.md", "size": 1024},
        ]
        self.assertIsNone(touch_release_service.find_firmware_asset(irrelevant_assets))

    @patch("core.touch_release_service.requests.get")
    def test_02_check_upstream_stable_priority_and_prerelease_opt_in(self, mock_get):
        """Verifies stable-priority filtering by default and inclusion when pre-releases are enabled."""
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = [
            {
                "tag_name": "v0.2.0-rc.20261120.1",
                "name": "StoryDeck Touch v0.2.0 RC 1",
                "prerelease": True,
                "draft": False,
                "published_at": "2026-11-20T10:00:00Z",
                "body": "Pre-release testing build",
                "assets": [
                    {"name": "storydeck-touch-v0.2.0-rc.20261120.1-firmware.bin", "size": 1500000, "browser_download_url": "http://dl/v0.2.0-rc.bin"}
                ]
            },
            {
                "tag_name": "v0.1.0",
                "name": "StoryDeck Touch v0.1.0 Stable",
                "prerelease": False,
                "draft": False,
                "published_at": "2026-11-01T10:00:00Z",
                "body": "First stable release",
                "assets": [
                    {"name": "storydeck-touch-v0.1.0-firmware.bin", "size": 1400000, "browser_download_url": "http://dl/v0.1.0.bin"}
                ]
            }
        ]
        mock_get.return_value = mock_resp

        # 1. Default check: include_prereleases=False
        res_stable = touch_release_service.check_upstream_touch_releases(include_prereleases=False, force=True)
        self.assertTrue(res_stable["ok"])
        self.assertEqual(len(res_stable["releases"]), 1)
        self.assertEqual(res_stable["releases"][0]["tag_name"], "v0.1.0")
        self.assertFalse(res_stable["releases"][0]["prerelease"])
        self.assertEqual(res_stable["latest_version"], "v0.1.0")
        self.assertEqual(res_stable["latest_stable_version"], "v0.1.0")
        self.assertTrue(res_stable["has_update"])  # Not yet in catalog

        # 2. Opt-in check: include_prereleases=True
        touch_release_service.invalidate_touch_release_cache()
        res_pre = touch_release_service.check_upstream_touch_releases(include_prereleases=True, force=True)
        self.assertTrue(res_pre["ok"])
        self.assertEqual(len(res_pre["releases"]), 2)
        tags = [r["tag_name"] for r in res_pre["releases"]]
        self.assertIn("v0.2.0-rc.20261120.1", tags)
        self.assertIn("v0.1.0", tags)
        # Stable release is prioritized for latest_version
        self.assertEqual(res_pre["latest_stable_version"], "v0.1.0")
        self.assertEqual(res_pre["latest_version"], "v0.1.0")

    @patch("core.touch_release_service.requests.get")
    def test_03_notification_only_guarantee(self, mock_get):
        """Verifies that release checks ONLY notify and never download files or alter SQLite records."""
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = [
            {
                "tag_name": "v0.1.0",
                "name": "StoryDeck Touch v0.1.0 Stable",
                "prerelease": False,
                "draft": False,
                "published_at": "2026-11-01T10:00:00Z",
                "body": "First stable release",
                "assets": [
                    {"name": "storydeck-touch-v0.1.0-firmware.bin", "size": 1400000, "browser_download_url": "http://dl/v0.1.0.bin"}
                ]
            }
        ]
        mock_get.return_value = mock_resp

        # Check releases
        res = touch_release_service.check_upstream_touch_releases(force=True)
        self.assertTrue(res["ok"])
        self.assertTrue(res["has_update"])

        # Verify no files were created in FIRMWARE_DIR
        fw_files = list(Path(core_config.FIRMWARE_DIR).glob("*.bin"))
        self.assertEqual(len(fw_files), 0, "No firmware binaries should be downloaded during check.")

        # Verify database remains empty
        cataloged = database.list_firmware_releases()
        self.assertEqual(len(cataloged), 0, "No catalog records should be added during release check.")

    @patch("core.touch_release_service.requests.get")
    def test_04_download_upstream_touch_release_valid(self, mock_get):
        """Verifies on-demand user-initiated download, 0xE9 validation, and SHA-256 cataloging."""
        bin_data = self._make_esp32_bin(b"STORYDECK_TOUCH_V0_1_0")
        expected_sha = hashlib.sha256(bin_data).hexdigest()

        def mock_get_router(url, *args, **kwargs):
            resp = MagicMock()
            if "releases/tags/v0.1.0" in url:
                resp.status_code = 200
                resp.json.return_value = {
                    "tag_name": "v0.1.0",
                    "name": "StoryDeck Touch v0.1.0",
                    "body": "Official stable release notes",
                    "assets": [
                        {
                            "name": "storydeck-touch-v0.1.0-firmware.bin",
                            "size": len(bin_data),
                            "browser_download_url": "https://github.com/YotoStoryDeck/StoryDeck-Touch/releases/download/v0.1.0/storydeck-touch-v0.1.0-firmware.bin"
                        }
                    ]
                }
                return resp
            elif "releases/download/v0.1.0" in url:
                resp.status_code = 200
                resp.iter_content.return_value = [bin_data]
                return resp
            resp.status_code = 404
            return resp

        mock_get.side_effect = mock_get_router

        # Trigger download
        dl_res = touch_release_service.download_upstream_touch_release("v0.1.0")
        self.assertTrue(dl_res["ok"])
        self.assertEqual(dl_res["version"], "v0.1.0")
        self.assertTrue(dl_res["is_active_latest"])

        # Check binary on disk
        target_bin = Path(core_config.FIRMWARE_DIR) / "firmware_v0.1.0.bin"
        self.assertTrue(target_bin.is_file())
        self.assertEqual(target_bin.read_bytes(), bin_data)

        # Check database record
        rec = database.get_firmware_release("v0.1.0")
        self.assertIsNotNone(rec)
        self.assertEqual(rec["sha256_hash"], expected_sha)
        self.assertEqual(rec["file_size_bytes"], len(bin_data))
        self.assertTrue(rec["is_active_latest"])

    @patch("core.touch_release_service.requests.get")
    def test_05_download_rejects_invalid_magic_header(self, mock_get):
        """Verifies rejection of non-ESP32 binary files (missing 0xE9 magic byte)."""
        bad_data = b"NOT_VALID_ESP32_IMAGE_HEADER_0000"

        def mock_get_router(url, *args, **kwargs):
            resp = MagicMock()
            if "releases/tags/v0.1.0" in url:
                resp.status_code = 200
                resp.json.return_value = {
                    "tag_name": "v0.1.0",
                    "assets": [
                        {"name": "firmware.bin", "size": len(bad_data), "browser_download_url": "http://dl/bad.bin"}
                    ]
                }
                return resp
            elif "http://dl/bad.bin" in url:
                resp.status_code = 200
                resp.iter_content.return_value = [bad_data]
                return resp
            resp.status_code = 404
            return resp

        mock_get.side_effect = mock_get_router

        dl_res = touch_release_service.download_upstream_touch_release("v0.1.0")
        self.assertFalse(dl_res["ok"])
        self.assertIn("missing 0xE9", dl_res["error"])

        # Ensure not stored in database
        self.assertIsNone(database.get_firmware_release("v0.1.0"))

    @patch("core.touch_release_service.requests.get")
    def test_06_admin_flask_endpoints(self, mock_get):
        """Tests /api/admin/firmware/upstream endpoints for authentication, querying, and preferences."""
        # Unauthenticated rejected
        unauth = self.client.get("/api/admin/firmware/upstream")
        self.assertIn(unauth.status_code, (401, 403))

        self._login_admin()

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = [
            {
                "tag_name": "v0.1.0",
                "name": "StoryDeck Touch v0.1.0",
                "prerelease": False,
                "draft": False,
                "published_at": "2026-11-01T10:00:00Z",
                "assets": [
                    {"name": "firmware.bin", "size": 1200000, "browser_download_url": "http://dl/fw.bin"}
                ]
            }
        ]
        mock_get.return_value = mock_resp

        # 1. GET /api/admin/firmware/upstream
        get_res = self.client.get("/api/admin/firmware/upstream?force=true")
        self.assertEqual(get_res.status_code, 200)
        data = get_res.get_json()
        self.assertTrue(data["ok"])
        self.assertEqual(data["latest_version"], "v0.1.0")

        # 2. POST /api/admin/firmware/upstream/preferences
        pref_res = self.client.post(
            "/api/admin/firmware/upstream/preferences",
            json={"include_prereleases": True},
        )
        self.assertEqual(pref_res.status_code, 200)
        self.assertTrue(pref_res.get_json()["touch_include_prereleases"])
        self.assertTrue(database.get_system_settings().get("touch_include_prereleases"))

        # 3. POST /api/admin/firmware/upstream/download with missing tag
        bad_dl = self.client.post("/api/admin/firmware/upstream/download", json={})
        self.assertEqual(bad_dl.status_code, 400)


if __name__ == "__main__":
    unittest.main()
