"""Unit tests for ESP32 OTA firmware, upstream GitHub releases, Touch SD cache relays, and device config."""
import hashlib
import io
from pathlib import Path
import unittest
from unittest.mock import MagicMock, patch

from tests.harness import IsolatedTestCase
from app import app
import core.config as core_config
from core import touch_release_service
import database


class TestOtaAndDeviceCacheRelays(IsolatedTestCase):
    def setUp(self):
        super().setUp()
        self.client = app.test_client()
        conn = database.get_db_connection()
        try:
            with conn:
                conn.execute("DELETE FROM firmware_releases;")
                conn.execute("DELETE FROM esp32_devices WHERE mac_address LIKE 'p5:%' OR mac_address LIKE 'kiosk-p5-%';")
        finally:
            conn.close()

        fw_dir = Path(core_config.FIRMWARE_DIR)
        if fw_dir.exists():
            for f in fw_dir.glob("*.bin"):
                f.unlink(missing_ok=True)

        database.register_esp32_device("p5:aa:bb:cc:dd:01", "Oliver's Bedside Display", "192.168.1.80", "v1.2.0")
        database.update_esp32_device("p5:aa:bb:cc:dd:01", {"is_approved": 1, "parent_pin": "4321"})

        database.register_esp32_device("p5:aa:bb:cc:dd:02", "Maya's Bedroom Display", "192.168.1.81", "v1.2.0")
        database.update_esp32_device("p5:aa:bb:cc:dd:02", {"is_approved": 1, "parent_pin": "1234"})

        database.register_esp32_device("p5:aa:bb:cc:dd:03", "Quarantined Display", "192.168.1.82", "v1.1.0")

        database.register_esp32_device("kiosk-p5-wall", "Playroom Wall Kiosk", "192.168.1.90", "v1.2.0-web")
        database.update_esp32_device("kiosk-p5-wall", {"is_approved": 1})

    def _login_admin(self):
        with self.client.session_transaction() as sess:
            sess["user_id"] = "p5_admin"
            sess["username"] = "admin"
            sess["role"] = "admin"

    def _make_esp32_bin(self, tag: bytes = b"TEST_BUILD") -> bytes:
        return b"\xe9\x04\x02\x20" + tag + (b"\xaa" * 64)

    def test_01_firmware_upload_list_set_latest_and_delete(self):
        """Verifies upload validation, SHA-256 cataloging, active-latest switching, and deletion cleanup."""
        unauth = self.client.get("/api/admin/firmware", environ_base={"REMOTE_ADDR": "192.168.1.50"})
        self.assertIn(unauth.status_code, (401, 403))

        with self.client.session_transaction() as sess:
            sess["user_id"] = "p5_member"
            sess["username"] = "sitter"
            sess["role"] = "member"
        self.assertEqual(self.client.get("/api/admin/firmware").status_code, 403)

        self._login_admin()

        bad_magic = self.client.post(
            "/api/admin/firmware/upload",
            data={"version": "v1.2.0", "file": (io.BytesIO(b"NOT_AN_ESP32_BINARY_PAYLOAD_12345"), "firmware.bin")},
            content_type="multipart/form-data",
        )
        self.assertEqual(bad_magic.status_code, 400)

        bad_ver = self.client.post(
            "/api/admin/firmware/upload",
            data={"version": "../evil", "file": (io.BytesIO(self._make_esp32_bin()), "firmware.bin")},
            content_type="multipart/form-data",
        )
        self.assertEqual(bad_ver.status_code, 400)

        bad_ext = self.client.post(
            "/api/admin/firmware/upload",
            data={"version": "v1.2.0", "file": (io.BytesIO(self._make_esp32_bin()), "firmware.txt")},
            content_type="multipart/form-data",
        )
        self.assertEqual(bad_ext.status_code, 400)

        bin_v120 = self._make_esp32_bin(b"V120")
        res1 = self.client.post(
            "/api/admin/firmware/upload",
            data={
                "version": "v1.2.0",
                "release_tag": "Previous Stable",
                "git_commit": "ede4c6c",
                "release_notes": "Initial SQLite release",
                "is_active_latest": "true",
                "file": (io.BytesIO(bin_v120), "firmware_v1.2.0.bin"),
            },
            content_type="multipart/form-data",
        )
        self.assertEqual(res1.status_code, 201)
        d1 = res1.get_json()
        self.assertTrue(d1["ok"])
        self.assertEqual(d1["release"]["sha256_hash"], hashlib.sha256(bin_v120).hexdigest())
        self.assertTrue(d1["release"]["is_active_latest"])
        p1 = Path(d1["release"]["file_path"])
        self.assertTrue(p1.is_file())
        self.assertEqual(p1.resolve().parent, Path(core_config.FIRMWARE_DIR).resolve())

        bin_v121 = self._make_esp32_bin(b"V121")
        res2 = self.client.post(
            "/api/admin/firmware/upload",
            data={
                "version": "v1.2.1",
                "release_tag": "Latest Stable",
                "git_commit": "7a35e53",
                "release_notes": "Dual-tab catalog + OTA rollback guard",
                "is_active_latest": "true",
                "file": (io.BytesIO(bin_v121), "firmware_v1.2.1.bin"),
            },
            content_type="multipart/form-data",
        )
        self.assertEqual(res2.status_code, 201)

        rels = {r["version"]: r for r in self.client.get("/api/admin/firmware").get_json()}
        self.assertTrue(rels["v1.2.1"]["is_active_latest"])
        self.assertFalse(rels["v1.2.0"]["is_active_latest"])

        self.assertEqual(self.client.post("/api/admin/firmware/v1.2.0/set-latest").status_code, 200)
        rels2 = {r["version"]: r for r in self.client.get("/api/admin/firmware").get_json()}
        self.assertTrue(rels2["v1.2.0"]["is_active_latest"])
        self.assertFalse(rels2["v1.2.1"]["is_active_latest"])

        self.assertEqual(self.client.delete("/api/admin/firmware/v1.2.0").status_code, 200)
        self.assertFalse(p1.exists())
        rels3 = {r["version"]: r for r in self.client.get("/api/admin/firmware").get_json()}
        self.assertNotIn("v1.2.0", rels3)
        self.assertTrue(rels3["v1.2.1"]["is_active_latest"])

    def test_02_firmware_binary_streaming_and_security(self):
        """Verifies trusted LAN ESP32 pull stream headers and WAN 403 block."""
        self._login_admin()
        bin_bytes = self._make_esp32_bin(b"STREAM_CHECK")
        up = self.client.post(
            "/api/admin/firmware/upload",
            data={"version": "v1.2.1", "is_active_latest": "true", "file": (io.BytesIO(bin_bytes), "firmware.bin")},
            content_type="multipart/form-data",
        )
        self.assertEqual(up.status_code, 201)

        with self.client.session_transaction() as sess:
            sess.clear()

        wan_res = self.client.get("/api/firmware/v1.2.1/firmware.bin", environ_base={"REMOTE_ADDR": "8.8.8.8"})
        self.assertEqual(wan_res.status_code, 403)
        wan_res.close()

        lan_res = self.client.get("/api/firmware/v1.2.1/firmware.bin", environ_base={"REMOTE_ADDR": "192.168.1.80"})
        self.assertEqual(lan_res.status_code, 200)
        body = lan_res.data
        lan_res.close()
        self.assertEqual(body, bin_bytes)
        self.assertEqual(lan_res.headers.get("X-SHA256"), hashlib.sha256(bin_bytes).hexdigest())
        self.assertEqual(lan_res.headers.get("X-Firmware-Version"), "v1.2.1")
        self.assertEqual(int(lan_res.headers.get("Content-Length", 0)), len(bin_bytes))

    def test_03_single_ota_push_rollback_error_and_update_all(self):
        """Tests 1-click OTA update, same-version gate, rollback, upstream failure, and update-all."""
        self._login_admin()
        self.client.post(
            "/api/admin/firmware/upload",
            data={"version": "v1.2.0", "is_active_latest": "false", "file": (io.BytesIO(self._make_esp32_bin(b"120")), "fw120.bin")},
            content_type="multipart/form-data",
        )
        up2 = self.client.post(
            "/api/admin/firmware/upload",
            data={"version": "v1.2.1", "is_active_latest": "true", "file": (io.BytesIO(self._make_esp32_bin(b"121")), "fw121.bin")},
            content_type="multipart/form-data",
        )
        sha121 = up2.get_json()["release"]["sha256_hash"]

        self.assertEqual(self.client.post("/api/admin/devices/p5:aa:bb:cc:dd:01/update", json={"version": "v1.2.0"}).status_code, 400)

        mock_ok = MagicMock(status_code=202)
        with patch("routes.admin_firmware.requests.post", return_value=mock_ok) as mock_post:
            res = self.client.post("/api/admin/devices/p5:aa:bb:cc:dd:01/update", json={"version": "v1.2.1"})
        self.assertEqual(res.status_code, 200)
        d = res.get_json()
        self.assertTrue(d["ok"])
        self.assertEqual(d["target"], "v1.2.1")
        self.assertEqual(d["previous"], "v1.2.0")
        call_args, call_kwargs = mock_post.call_args
        self.assertEqual(call_args[0], "http://192.168.1.80/ota/trigger")
        self.assertEqual(call_kwargs["json"]["url"], "/api/firmware/v1.2.1/firmware.bin")
        self.assertEqual(call_kwargs["json"]["sha256"], sha121)

        dev1 = database.get_esp32_device("p5:aa:bb:cc:dd:01")
        self.assertEqual(dev1["ota_status"], "pending")
        self.assertEqual(dev1["target_firmware_version"], "v1.2.1")
        self.assertEqual(dev1["previous_firmware_version"], "v1.2.0")

        import requests as req_mod
        with patch("routes.admin_firmware.requests.post", side_effect=req_mod.ConnectionError("timeout")):
            fail_res = self.client.post("/api/admin/devices/p5:aa:bb:cc:dd:01/update", json={"version": "v1.2.1", "force": True})
        self.assertEqual(fail_res.status_code, 502)
        dev1_fail = database.get_esp32_device("p5:aa:bb:cc:dd:01")
        self.assertEqual(dev1_fail["ota_status"], "failed")
        self.assertIn("timeout", dev1_fail["ota_last_error"])

        with patch("routes.admin_firmware.requests.post", return_value=mock_ok) as mock_batch:
            all_res = self.client.post("/api/admin/devices/update-all", json={"version": "v1.2.1", "delay_seconds": 0})
        self.assertEqual(all_res.status_code, 200)
        all_data = all_res.get_json()
        self.assertEqual(all_data["dispatched"], 2)
        self.assertEqual(all_data["skipped"], 2)
        dispatched_urls = [c.args[0] for c in mock_batch.call_args_list]
        self.assertIn("http://192.168.1.80/ota/trigger", dispatched_urls)
        self.assertIn("http://192.168.1.81/ota/trigger", dispatched_urls)

    def test_04_ota_progress_telemetry_and_boot_verification_rollback(self):
        """Verifies /ota-progress updates DB, WAN blocked, and register boot_state handles verified/rolled_back."""
        database.update_esp32_device("p5:aa:bb:cc:dd:01", {
            "target_firmware_version": "v1.2.1",
            "ota_status": "pending",
            "ota_progress": 0,
        })

        wan = self.client.post(
            "/api/devices/p5:aa:bb:cc:dd:01/ota-progress",
            json={"status": "flashing", "percent": 50},
            environ_base={"REMOTE_ADDR": "8.8.8.8"},
        )
        self.assertEqual(wan.status_code, 403)

        prog_res = self.client.post(
            "/api/devices/p5:aa:bb:cc:dd:01/ota-progress",
            json={"status": "flashing", "percent": 68},
            environ_base={"REMOTE_ADDR": "192.168.1.80"},
        )
        self.assertEqual(prog_res.status_code, 200)
        dev = database.get_esp32_device("p5:aa:bb:cc:dd:01")
        self.assertEqual(dev["ota_status"], "flashing")
        self.assertEqual(dev["ota_progress"], 68)

        reg_ok = self.client.post("/api/devices/register", json={
            "mac_address": "p5:aa:bb:cc:dd:01",
            "ip_address": "192.168.1.80",
            "firmware_version": "v1.2.1",
            "boot_state": "verified",
        })
        self.assertEqual(reg_ok.status_code, 200)
        dev_verified = database.get_esp32_device("p5:aa:bb:cc:dd:01")
        self.assertEqual(dev_verified["firmware_version"], "v1.2.1")
        self.assertEqual(dev_verified["ota_status"], "idle")
        self.assertEqual(dev_verified["ota_progress"], 100)

        reg_rb = self.client.post("/api/devices/register", json={
            "mac_address": "p5:aa:bb:cc:dd:01",
            "ip_address": "192.168.1.80",
            "firmware_version": "v1.2.0",
            "boot_state": "rolled_back",
        })
        self.assertEqual(reg_rb.status_code, 200)
        dev_rb = database.get_esp32_device("p5:aa:bb:cc:dd:01")
        self.assertEqual(dev_rb["firmware_version"], "v1.2.0")
        self.assertEqual(dev_rb["ota_status"], "rolled_back")

    def test_05_touch_microsd_cache_relay_and_ssrf_guards(self):
        """Verifies /cache/status, /cache/prune, /cache/refresh, DELETE /cache relays & SSRF guards in admin_device_cache."""
        self._login_admin()

        self.assertEqual(self.client.post("/api/admin/devices/p5:aa:bb:cc:dd:03/cache/prune", json={}).status_code, 400)
        self.assertEqual(self.client.post("/api/admin/devices/kiosk-p5-wall/cache/prune", json={}).status_code, 400)

        database.register_esp32_device("p5:aa:bb:cc:dd:99", "Bad IP Unit", "8.8.4.4", "v1.2.0")
        database.update_esp32_device("p5:aa:bb:cc:dd:99", {"is_approved": 1})
        self.assertEqual(self.client.get("/api/admin/devices/p5:aa:bb:cc:dd:99/cache/status").status_code, 400)

        m_status = MagicMock(status_code=200)
        m_status.json.return_value = {"mounted": True, "used_mb": 42, "files": 128, "orphans": 4}
        with patch("routes.admin_device_cache.requests.request", return_value=m_status) as mock_req:
            st_res = self.client.get("/api/admin/devices/p5:aa:bb:cc:dd:01/cache/status")
        self.assertEqual(st_res.status_code, 200)
        self.assertEqual(st_res.get_json()["sd"]["orphans"], 4)
        self.assertEqual(mock_req.call_args.kwargs["method"], "GET")
        self.assertEqual(mock_req.call_args.kwargs["url"], "http://192.168.1.80/sd/status")

        m_prune = MagicMock(status_code=200)
        m_prune.json.return_value = {"ok": True, "deleted_count": 4, "reclaimed_bytes": 286720}
        with patch("routes.admin_device_cache.requests.request", return_value=m_prune) as mock_req:
            pr_res = self.client.post("/api/admin/devices/p5:aa:bb:cc:dd:01/cache/prune", json={})
        self.assertEqual(pr_res.status_code, 200)
        self.assertEqual(pr_res.get_json()["device_response"]["deleted_count"], 4)
        self.assertEqual(mock_req.call_args.kwargs["method"], "POST")
        self.assertEqual(mock_req.call_args.kwargs["url"], "http://192.168.1.80/sd/prune")
        self.assertEqual(mock_req.call_args.kwargs["params"], {"pin": "4321"})

        m_ref = MagicMock(status_code=200)
        m_ref.json.return_value = {"ok": True, "card_id": "abc123"}
        with patch("routes.admin_device_cache.requests.request", return_value=m_ref) as mock_req:
            rf_res = self.client.post("/api/devices/p5:aa:bb:cc:dd:01/cache/refresh", json={"card_id": "abc123"})
        self.assertEqual(rf_res.status_code, 200)
        self.assertEqual(mock_req.call_args.kwargs["url"], "http://192.168.1.80/sd/refresh")
        self.assertEqual(mock_req.call_args.kwargs["params"], {"pin": "4321", "id": "abc123"})

        m_del = MagicMock(status_code=200)
        m_del.json.return_value = {"ok": True, "cleared_dir": "/ythumbs", "total_deleted": 128}
        with patch("routes.admin_device_cache.requests.request", return_value=m_del) as mock_req:
            dl_res = self.client.delete("/api/admin/devices/p5:aa:bb:cc:dd:01/cache")
        self.assertEqual(dl_res.status_code, 200)
        self.assertEqual(mock_req.call_args.kwargs["method"], "DELETE")
        self.assertEqual(mock_req.call_args.kwargs["url"], "http://192.168.1.80/sd/cache")
        self.assertEqual(mock_req.call_args.kwargs["params"], {"pin": "4321", "confirm": "1"})

    def test_06_admin_html_contains_firmware_and_cache_ui(self):
        """Verifies /admin HTML renders Firmware Repository, OTA modals, and firmware.js script."""
        res = self.client.get("/admin")
        self.assertEqual(res.status_code, 200)
        html = res.get_data(as_text=True)
        for token in (
            'id="uploadFirmwareModal"',
            'id="pushFirmwareModal"',
            'id="firmwareReleasesTable"',
            'openUploadFirmwareModal()',
            'pushFirmwareUpdateAll()',
            '/static/js/admin/firmware.js?v=',
        ):
            self.assertIn(token, html)

    def test_07_device_config_and_settings_sync(self):
        """Covers GET /api/device/config and PUT /api/device/settings bi-directional sync."""
        self._login_admin()
        cfg_res = self.client.get("/api/device/config?device_id=kiosk-p5-wall")
        self.assertEqual(cfg_res.status_code, 200)
        cfg = cfg_res.get_json()
        self.assertTrue(cfg["is_approved"])

        put_res = self.client.put("/api/device/settings", json={
            "device_id": "kiosk-p5-wall",
            "parent_pin": "2468",
            "lock_player": True,
            "locked_player_id": "player_1",
            "settings": {"keepAwake": False, "sleepTimerMin": 15},
        })
        self.assertEqual(put_res.status_code, 200)
        updated_cfg = self.client.get("/api/device/config?device_id=kiosk-p5-wall").get_json()
        self.assertEqual(updated_cfg["parent_pin"], "2468")
        self.assertTrue(updated_cfg["lock_player"])
        self.assertEqual(updated_cfg["settings"]["sleepTimerMin"], 15)


class TestTouchReleaseService(IsolatedTestCase):
    def setUp(self):
        super().setUp()
        self.client = app.test_client()
        touch_release_service.invalidate_touch_release_cache()
        conn = database.get_db_connection()
        try:
            with conn:
                conn.execute("DELETE FROM firmware_releases;")
                conn.execute("DELETE FROM esp32_devices WHERE mac_address LIKE 'test:touch:%';")
                conn.execute("DELETE FROM system_settings WHERE key = 'touch_include_prereleases';")
        finally:
            conn.close()

        fw_dir = Path(core_config.FIRMWARE_DIR)
        if fw_dir.exists():
            for f in fw_dir.glob("firmware_*.bin"):
                f.unlink(missing_ok=True)

    def tearDown(self):
        touch_release_service.invalidate_touch_release_cache()
        super().tearDown()

    def _login_admin(self):
        with self.client.session_transaction() as sess:
            sess["user_id"] = "admin_user"
            sess["username"] = "admin"
            sess["role"] = "admin"

    def _make_esp32_bin(self, tag: bytes = b"TOUCH_UPSTREAM_TEST") -> bytes:
        return b"\xe9\x04\x02\x20" + tag + (b"\xbb" * 64)

    def test_01_find_firmware_asset(self):
        """Verifies accurate identification of the firmware application binary asset."""
        assets = [
            {"name": "storydeck-touch-v0.1.0-bootloader.bin", "size": 16384, "browser_download_url": "http://example.com/bootloader.bin"},
            {"name": "storydeck-touch-v0.1.0-partitions.bin", "size": 4096, "browser_download_url": "http://example.com/partitions.bin"},
            {"name": "storydeck-touch-v0.1.0-firmware.bin", "size": 1843200, "browser_download_url": "http://example.com/fw.bin"},
            {"name": "storydeck-touch-v0.1.0-factory.bin", "size": 2097152, "browser_download_url": "http://example.com/factory.bin"},
        ]
        self.assertEqual(touch_release_service.find_firmware_asset(assets)["name"], "storydeck-touch-v0.1.0-firmware.bin")

        simple_assets = [{"name": "bootloader.bin", "size": 16384}, {"name": "firmware.bin", "size": 1500000}]
        self.assertEqual(touch_release_service.find_firmware_asset(simple_assets)["name"], "firmware.bin")

        irrelevant_assets = [{"name": "bootloader.bin", "size": 16384}, {"name": "README.md", "size": 1024}]
        self.assertIsNone(touch_release_service.find_firmware_asset(irrelevant_assets))

    @patch("core.touch_release_service.requests.get")
    def test_02_check_upstream_stable_priority_and_prerelease_opt_in(self, mock_get):
        """Verifies stable-priority filtering by default and inclusion when pre-releases are enabled."""
        mock_resp = MagicMock(status_code=200)
        mock_resp.json.return_value = [
            {
                "tag_name": "v0.2.0-rc.20261120.1",
                "name": "StoryDeck Touch v0.2.0 RC 1",
                "prerelease": True,
                "draft": False,
                "published_at": "2026-11-20T10:00:00Z",
                "body": "Pre-release testing build",
                "assets": [{"name": "storydeck-touch-v0.2.0-rc.20261120.1-firmware.bin", "size": 1500000, "browser_download_url": "http://dl/v0.2.0-rc.bin"}],
            },
            {
                "tag_name": "v0.1.0",
                "name": "StoryDeck Touch v0.1.0 Stable",
                "prerelease": False,
                "draft": False,
                "published_at": "2026-11-01T10:00:00Z",
                "body": "First stable release",
                "assets": [{"name": "storydeck-touch-v0.1.0-firmware.bin", "size": 1400000, "browser_download_url": "http://dl/v0.1.0.bin"}],
            },
        ]
        mock_get.return_value = mock_resp

        res_stable = touch_release_service.check_upstream_touch_releases(include_prereleases=False, force=True)
        self.assertTrue(res_stable["ok"])
        self.assertEqual(len(res_stable["releases"]), 1)
        self.assertEqual(res_stable["releases"][0]["tag_name"], "v0.1.0")
        self.assertEqual(res_stable["latest_version"], "v0.1.0")
        self.assertTrue(res_stable["has_update"])

        touch_release_service.invalidate_touch_release_cache()
        res_pre = touch_release_service.check_upstream_touch_releases(include_prereleases=True, force=True)
        self.assertTrue(res_pre["ok"])
        self.assertEqual(len(res_pre["releases"]), 2)
        self.assertEqual(res_pre["latest_version"], "v0.1.0")

    @patch("core.touch_release_service.requests.get")
    def test_03_notification_only_guarantee(self, mock_get):
        """Verifies that release checks ONLY notify and never download files or alter SQLite records."""
        mock_resp = MagicMock(status_code=200)
        mock_resp.json.return_value = [
            {
                "tag_name": "v0.1.0",
                "name": "StoryDeck Touch v0.1.0 Stable",
                "prerelease": False,
                "draft": False,
                "published_at": "2026-11-01T10:00:00Z",
                "assets": [{"name": "storydeck-touch-v0.1.0-firmware.bin", "size": 1400000, "browser_download_url": "http://dl/v0.1.0.bin"}],
            }
        ]
        mock_get.return_value = mock_resp

        res = touch_release_service.check_upstream_touch_releases(force=True)
        self.assertTrue(res["ok"])
        self.assertTrue(res["has_update"])
        self.assertEqual(len(list(Path(core_config.FIRMWARE_DIR).glob("*.bin"))), 0)
        self.assertEqual(len(database.list_firmware_releases()), 0)

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
                    "assets": [{"name": "storydeck-touch-v0.1.0-firmware.bin", "size": len(bin_data), "browser_download_url": "https://github.com/YotoStoryDeck/StoryDeck-Touch/releases/download/v0.1.0/storydeck-touch-v0.1.0-firmware.bin"}],
                }
                return resp
            elif "releases/download/v0.1.0" in url:
                resp.status_code = 200
                resp.iter_content.return_value = [bin_data]
                return resp
            resp.status_code = 404
            return resp

        mock_get.side_effect = mock_get_router

        dl_res = touch_release_service.download_upstream_touch_release("v0.1.0")
        self.assertTrue(dl_res["ok"])
        self.assertEqual(dl_res["version"], "v0.1.0")
        self.assertTrue(dl_res["is_active_latest"])

        target_bin = Path(core_config.FIRMWARE_DIR) / "firmware_v0.1.0.bin"
        self.assertTrue(target_bin.is_file())
        self.assertEqual(target_bin.read_bytes(), bin_data)

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
                    "assets": [{"name": "firmware.bin", "size": len(bad_data), "browser_download_url": "http://dl/bad.bin"}],
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
        self.assertIsNone(database.get_firmware_release("v0.1.0"))

    @patch("core.touch_release_service.requests.get")
    def test_06_admin_flask_endpoints(self, mock_get):
        """Tests /api/admin/firmware/upstream endpoints for authentication, querying, and preferences."""
        self.assertIn(self.client.get("/api/admin/firmware/upstream").status_code, (401, 403))

        self._login_admin()
        mock_resp = MagicMock(status_code=200)
        mock_resp.json.return_value = [
            {
                "tag_name": "v0.1.0",
                "name": "StoryDeck Touch v0.1.0",
                "prerelease": False,
                "draft": False,
                "published_at": "2026-11-01T10:00:00Z",
                "assets": [{"name": "firmware.bin", "size": 1200000, "browser_download_url": "http://dl/fw.bin"}],
            }
        ]
        mock_get.return_value = mock_resp

        get_res = self.client.get("/api/admin/firmware/upstream?force=true")
        self.assertEqual(get_res.status_code, 200)
        self.assertEqual(get_res.get_json()["latest_version"], "v0.1.0")

        pref_res = self.client.post("/api/admin/firmware/upstream/preferences", json={"include_prereleases": True})
        self.assertEqual(pref_res.status_code, 200)
        self.assertTrue(pref_res.get_json()["touch_include_prereleases"])
        self.assertTrue(database.get_system_settings().get("touch_include_prereleases"))

        self.assertEqual(self.client.post("/api/admin/firmware/upstream/download", json={}).status_code, 400)


if __name__ == "__main__":
    unittest.main()
