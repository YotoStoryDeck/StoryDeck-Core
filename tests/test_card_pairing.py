import unittest
from unittest.mock import MagicMock, patch

import tests.test_isolation  # noqa: F401 - must import first to sandbox SQLite DB
import app as app_module
import database
import routes.bridge_cards as bridge_cards_module


class TestCardPairingEndpoints(unittest.TestCase):
    def setUp(self):
        self.client = app_module.app.test_client()
        database.init_db()
        if not database.get_user_by_username("pair_admin"):
            database.create_admin_user("pair_admin", "StrongPass123!", "Pair Admin", role="admin")
        database.upsert_profile("child_a", {
            "display_name": "Child A",
            "default_playback_mode": "streaming",
        })
        self.client.post("/api/admin/auth/login", json={"username": "pair_admin", "password": "StrongPass123!"})

    def test_01_detect_no_card_inserted(self):
        with patch("core.player_service.get_player_status", return_value={"online": True, "card_id": ""}):
            res = self.client.get("/api/cards/detect?device_id=child-a-mini")
            self.assertEqual(res.status_code, 200)
            data = res.get_json()
            self.assertTrue(data["ok"])
            self.assertFalse(data["detected"])

    def test_02_detect_editable_myo_card(self):
        fake_yoto = MagicMock()
        fake_yoto.get_all_cloud_playlists.return_value = [{"cardId": "myo123", "title": "My MYO"}]
        fake_yoto.get_card_detail.return_value = {"title": "My MYO", "userId": "user_abc", "isMyo": True}
        with patch("core.player_service.get_player_status", return_value={"online": True, "card_id": "myo123", "title": "My MYO"}), \
             patch.object(bridge_cards_module.clients, "yoto_client", fake_yoto):
            res = self.client.get("/api/cards/detect?device_id=child-a-mini")
            self.assertEqual(res.status_code, 200)
            data = res.get_json()
            self.assertTrue(data["detected"])
            self.assertTrue(data["is_editable"])
            self.assertFalse(data["is_locked"])

    def test_03_detect_locked_retail_card(self):
        fake_yoto = MagicMock()
        fake_yoto.get_all_cloud_playlists.return_value = []
        fake_yoto.get_card_detail.return_value = {"title": "Charlie Brown", "userId": "yoto", "isMyo": False}
        with patch("core.player_service.get_player_status", return_value={"online": True, "card_id": "8KkQc", "title": "Charlie Brown"}), \
             patch.object(bridge_cards_module.clients, "yoto_client", fake_yoto):
            res = self.client.get("/api/cards/detect?device_id=child-a-mini")
            self.assertEqual(res.status_code, 200)
            data = res.get_json()
            self.assertTrue(data["detected"])
            self.assertFalse(data["is_editable"])
            self.assertTrue(data["is_locked"])

    def test_04_pair_profile_and_fleet_cards(self):
        res_prof = self.client.post("/api/cards/pair", json={
            "card_id": "myo_child_a_01",
            "target": "profile",
            "profile_id": "child_a",
            "name": "Child A Primary",
        })
        self.assertEqual(res_prof.status_code, 200)
        self.assertTrue(res_prof.get_json()["ok"])
        prof = database.get_profile_by_id("child_a")
        self.assertEqual(prof.get("primary_card_id"), "myo_child_a_01")

        res_fleet = self.client.post("/api/cards/pair", json={
            "card_id": "myo_fleet_01",
            "target": "fleet",
            "name": "Car Trip Card",
        })
        self.assertEqual(res_fleet.status_code, 201)
        self.assertTrue(res_fleet.get_json()["ok"])


if __name__ == "__main__":
    unittest.main()
