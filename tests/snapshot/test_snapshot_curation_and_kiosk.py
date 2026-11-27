"""Snapshot tests for real curation rules, profile catalogs, and Kiosk/Touch endpoints."""

import database
from app import app
from tests.harness import SnapshotTestCase, TEST_MASTER_API_KEY


class TestSnapshotCurationAndKiosk(SnapshotTestCase):
    """Exercise curation, rotation limits, and endpoints on sanitized local DB data."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.client = app.test_client()
        cls.api_headers = {"X-Api-Key": TEST_MASTER_API_KEY}

    def test_01_catalog_matrix_and_profiles_load_cleanly(self):
        profiles = database.get_all_profiles()
        self.assertIsInstance(profiles, dict)
        self.assertGreaterEqual(len(profiles), 1)

        matrix = database.get_catalog_matrix()
        self.assertIsInstance(matrix, dict)
        self.assertIn("items", matrix)
        self.assertIn("profiles", matrix)
        self.assertGreater(len(matrix["items"]), 0)

    def test_02_touch_and_kiosk_catalog_endpoints_for_sanitized_profiles(self):
        profiles = database.get_all_profiles()
        for pid, pinfo in profiles.items():
            limit = max(1, int(pinfo.get("catalog_rotation_limit") or 12))

            # ESP32 Touch endpoint returns a JSON list of curated books
            touch_resp = self.client.get(
                f"/api/kids/{pid}/abs?client=touch", headers=self.api_headers
            )
            self.assertEqual(touch_resp.status_code, 200)
            touch_items = touch_resp.get_json()
            self.assertIsInstance(touch_items, list)
            # Every item carries standard catalog keys
            for item in touch_items[:5]:
                self.assertIn("id", set(item.keys()))
                self.assertIn("title", set(item.keys()))
            self.assertGreaterEqual(limit, 1)

            # Kiosk endpoint returns {"abs": [...], "yoto": [...]}
            kiosk_resp = self.client.get(
                f"/api/kids/{pid}/catalog?client=kiosk", headers=self.api_headers
            )
            self.assertEqual(kiosk_resp.status_code, 200)
            kiosk_data = kiosk_resp.get_json()
            self.assertIsInstance(kiosk_data, dict)
            self.assertIn("abs", set(kiosk_data.keys()))
            self.assertIn("yoto", set(kiosk_data.keys()))
            self.assertIsInstance(kiosk_data["abs"], list)
            self.assertIsInstance(kiosk_data["yoto"], list)

            # Portal endpoint returns a JSON list of curated profile items
            portal_resp = self.client.get(
                f"/api/library/profile/{pid}", headers=self.api_headers
            )
            self.assertEqual(portal_resp.status_code, 200)
            portal_items = portal_resp.get_json()
            self.assertEqual(type(portal_items), list)

    def test_03_standalone_kiosk_catalog_and_admin_matrix_endpoint(self):
        devices = database.list_esp32_devices() or []
        dev_param = f"&device_id={devices[0]['mac_address']}" if devices else ""
        resp = self.client.get(
            f"/api/kids/__all__/catalog?client=kiosk{dev_param}",
            headers=self.api_headers,
        )
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertIsInstance(data, dict)
        self.assertIn("abs", set(data.keys()))
        self.assertIn("yoto", set(data.keys()))
        self.assertIsInstance(data["abs"], list)
        self.assertIsInstance(data["yoto"], list)

        matrix_resp = self.client.get(
            "/api/admin/catalog/matrix", headers=self.api_headers
        )
        self.assertEqual(matrix_resp.status_code, 200)
        matrix_json = matrix_resp.get_json()
        self.assertIsInstance(matrix_json, dict)
        self.assertIn("items", set(matrix_json.keys()))
