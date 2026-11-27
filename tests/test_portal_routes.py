"""
Unit tests for Mobile Parent Portal & Player endpoints.
Verifies GET / serves portal.html with LAN auth, and player transport endpoints.
"""
import tests.test_isolation  # noqa: F401 - must precede app/database/clients imports
import threading
import time
import unittest
from unittest.mock import MagicMock, patch
import requests
from app import app
import database
import clients
from clients.yoto_client import YotoClient


class TestPortalRoutes(unittest.TestCase):
    def setUp(self):
        self.app = app.test_client()
        self.app_context = app.app_context()
        self.app_context.push()

        # Seed profile
        conn = database.get_db_connection()
        try:
            with conn:
                cur = conn.cursor()
                cur.execute("DELETE FROM profiles WHERE id = 'portal_child';")
                cur.execute("""
                    INSERT INTO profiles (id, display_name, default_playback_mode, offline_retention_limit)
                    VALUES ('portal_child', 'Portal Child', 'streaming', 8);
                """)
        finally:
            conn.close()

    def tearDown(self):
        conn = database.get_db_connection()
        try:
            with conn:
                cur = conn.cursor()
                cur.execute("DELETE FROM profiles WHERE id = 'portal_child';")
        finally:
            conn.close()
        self.app_context.pop()

    def test_root_portal_serves_html_for_trusted_lan(self):
        """Verifies GET / serves portal.html for LAN clients (127.0.0.1)."""
        res = self.app.get("/", environ_base={"REMOTE_ADDR": "192.168.1.100"})
        self.assertEqual(res.status_code, 200)
        self.assertIn(b"StoryDeck \xe2\x80\xa2 Parent Portal", res.data)
        self.assertIn(b"Target Yoto Player", res.data)
        self.assertIn(b"Physical Cards Inventory", res.data)

    def test_player_devices_endpoint(self):
        """Verifies GET /api/player/devices returns device list."""
        mock_map = {
            "yoto_123": {
                "id": "yoto_123",
                "deviceId": "yoto_123",
                "name": "Bedroom Yoto",
                "online": True,
                "status": "idle"
            }
        }
        with patch("core.player_service.get_live_players_map", return_value=mock_map):
            res = self.app.get("/api/player/devices", environ_base={"REMOTE_ADDR": "192.168.1.100"})
            self.assertEqual(res.status_code, 200)
            data = res.get_json()
            self.assertEqual(len(data), 1)
            self.assertEqual(data[0]["name"], "Bedroom Yoto")

    def test_player_transport_endpoint(self):
        """Verifies POST /api/player/transport forwards actions to dispatch_player_control."""
        with patch("core.player_service.dispatch_player_control", return_value=(True, {"ok": True}, 200)) as mock_ctl:
            # Transport command
            res = self.app.post("/api/player/transport",
                                json={"device_id": "yoto_123", "action": "pause"},
                                environ_base={"REMOTE_ADDR": "192.168.1.100"})
            self.assertEqual(res.status_code, 200)
            mock_ctl.assert_called_with("yoto_123", "pause", volume=None, minutes=None, position=None, speed=None)

            # Volume command
            res = self.app.post("/api/player/transport",
                                json={"device_id": "yoto_123", "action": "volume", "volume": 65},
                                environ_base={"REMOTE_ADDR": "192.168.1.100"})
            self.assertEqual(res.status_code, 200)
            mock_ctl.assert_called_with("yoto_123", "volume", volume=65, minutes=None, position=None, speed=None)

    def test_library_profile_endpoint(self):
        """Verifies GET /api/library/profile/<id> returns combined ABS + Yoto catalog array (matching Kiosk)."""
        conn = database.get_db_connection()
        try:
            with conn:
                conn.execute(
                    "INSERT OR REPLACE INTO media_items (id, type, title, author, raw_metadata) VALUES (?, ?, ?, ?, ?);",
                    ("portal_abs_1", "abs", "Portal ABS Book", "Author A", "{}"),
                )
                conn.execute(
                    "INSERT OR REPLACE INTO media_items (id, type, title, author, raw_metadata) VALUES (?, ?, ?, ?, ?);",
                    ("yotoC1", "yoto", "Portal Yoto Card", "Yoto", "{}"),
                )
                conn.execute(
                    "INSERT OR REPLACE INTO profile_media_permissions (profile_id, media_id, permission) VALUES (?, ?, 'allowed');",
                    ("portal_child", "portal_abs_1"),
                )
                conn.execute(
                    "INSERT OR REPLACE INTO profile_media_permissions (profile_id, media_id, permission) VALUES (?, ?, 'allowed');",
                    ("portal_child", "yotoC1"),
                )
        finally:
            conn.close()

        res = self.app.get("/api/library/profile/portal_child", environ_base={"REMOTE_ADDR": "192.168.1.100"})
        self.assertEqual(res.status_code, 200)
        items = res.get_json()
        self.assertIsInstance(items, list)
        types = {it.get("type") for it in items}
        self.assertIn("abs", types)
        self.assertIn("yoto", types)

    def test_yoto_auth_status_invalid_grant_and_negative_cache(self):
        """Verifies GET /api/yoto/auth-status reports invalid_grant and honors 30s negative cache."""
        yc = YotoClient(client_id="cid_test", refresh_token="bad_ref")
        mock_res = MagicMock()
        mock_res.status_code = 400
        mock_res.text = '{"error":"invalid_grant","error_description":"Unknown or invalid refresh token."}'
        mock_res.json.return_value = {
            "error": "invalid_grant",
            "error_description": "Unknown or invalid refresh token."
        }

        with patch.object(clients, "yoto_client", yc), \
             patch("requests.post", return_value=mock_res) as mock_post:
            res1 = self.app.get("/api/yoto/auth-status", environ_base={"REMOTE_ADDR": "192.168.1.100"})
            self.assertEqual(res1.status_code, 200)
            d1 = res1.get_json()
            self.assertFalse(d1["token_valid"])
            self.assertFalse(d1["connected"])
            self.assertEqual(d1["reason"], "invalid_grant")
            self.assertEqual(mock_post.call_count, 1)

            # Immediate repeat without force=1 uses 30s negative cache
            res2 = self.app.get("/api/yoto/auth-status", environ_base={"REMOTE_ADDR": "192.168.1.100"})
            self.assertEqual(res2.status_code, 200)
            self.assertFalse(res2.get_json()["token_valid"])
            self.assertEqual(mock_post.call_count, 1)

            # force=1 bypasses negative cache
            res3 = self.app.get("/api/yoto/auth-status?force=1", environ_base={"REMOTE_ADDR": "192.168.1.100"})
            self.assertEqual(res3.status_code, 200)
            self.assertEqual(mock_post.call_count, 2)

    def test_yoto_auth_status_valid_token(self):
        """Verifies GET /api/yoto/auth-status succeeds and persists rotated refresh_token to temp DB."""
        orig_creds = database.get_api_credentials()
        yc = YotoClient(client_id="cid_test", refresh_token="init_ref_1")
        mock_res = MagicMock()
        mock_res.status_code = 200
        mock_res.json.return_value = {
            "access_token": "tok_ok",
            "refresh_token": "rot_ref_2",
            "expires_in": 3600
        }

        try:
            database.update_api_credentials({"yoto_client_id": "cid_test", "yoto_refresh_token": "init_ref_1"})
            with patch.object(clients, "yoto_client", yc), \
                 patch("requests.post", return_value=mock_res):
                res = self.app.get("/api/yoto/auth-status", environ_base={"REMOTE_ADDR": "192.168.1.100"})
                self.assertEqual(res.status_code, 200)
                data = res.get_json()
                self.assertTrue(data["token_valid"])
                self.assertTrue(data["connected"])
                self.assertEqual(data["reason"], "ok")
                self.assertEqual(database.get_api_credentials().get("yoto_refresh_token"), "rot_ref_2")
        finally:
            database.update_api_credentials(orig_creds)

    def test_yoto_get_token_thread_safe_single_refresh(self):
        """Verifies concurrent get_token() calls perform only a single HTTP refresh under lock."""
        yc = YotoClient(client_id="cid_concurrent", refresh_token="ref_conc_1")
        mock_res = MagicMock()
        mock_res.status_code = 200
        mock_res.json.return_value = {
            "access_token": "shared_access_tok",
            "refresh_token": "ref_conc_2",
            "expires_in": 3600
        }

        def slow_post(*args, **kwargs):
            time.sleep(0.05)
            return mock_res

        results = []
        errors = []

        def worker():
            try:
                results.append(yc.get_token())
            except Exception as exc:
                errors.append(exc)

        with patch("requests.post", side_effect=slow_post) as mock_post, \
             patch("database.update_api_credentials", return_value=True):
            threads = [threading.Thread(target=worker) for _ in range(6)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()

            self.assertEqual(errors, [])
            self.assertEqual(mock_post.call_count, 1)
            self.assertEqual(results, ["shared_access_tok"] * 6)

    def test_root_portal_contains_yoto_auth_lockout_ui(self):
        """Verifies GET / includes the Yoto token status pill and portal lockout panel."""
        res = self.app.get("/", environ_base={"REMOTE_ADDR": "192.168.1.100"})
        self.assertEqual(res.status_code, 200)
        self.assertIn(b"yotoAuthStatusPill", res.data)
        self.assertIn(b"yotoAuthBlockedView", res.data)
        self.assertIn(b"Yoto API Token Not Resolving", res.data)
        self.assertIn(b"btnRetryYotoAuth", res.data)

    def test_yoto_auth_status_syncs_refresh_token_from_sqlite(self):
        """Verifies YotoClient.get_auth_status() picks up a persisted refresh_token from SQLite instead of returning missing_credentials."""
        orig_creds = database.get_api_credentials()
        yc = YotoClient(client_id="cid_sync", refresh_token="")
        mock_res = MagicMock()
        mock_res.status_code = 200
        mock_res.json.return_value = {
            "access_token": "tok_synced",
            "refresh_token": "rot_ref_synced",
            "expires_in": 3600
        }

        try:
            database.update_api_credentials({"yoto_client_id": "cid_sync", "yoto_refresh_token": "db_ref_1"})
            with patch.object(clients, "yoto_client", yc), \
                 patch("requests.post", return_value=mock_res):
                res = self.app.get("/api/yoto/auth-status", environ_base={"REMOTE_ADDR": "192.168.1.100"})
                self.assertEqual(res.status_code, 200)
                data = res.get_json()
                self.assertTrue(data["token_valid"])
                self.assertTrue(data["connected"])
                self.assertEqual(data["reason"], "ok")
        finally:
            database.update_api_credentials(orig_creds)

    def test_portal_and_admin_share_series_utils_and_sort_controls(self):
        """Verifies portal library exposes X-Exclude-Single-Card-Series header and both portals load series_utils.js + sort controls."""
        old_val = database.get_system_settings().get("exclude_single_card_series")
        try:
            database.set_system_setting("exclude_single_card_series", True)
            res_on = self.app.get("/api/library/profile/portal_child", environ_base={"REMOTE_ADDR": "192.168.1.100"})
            self.assertEqual(res_on.status_code, 200)
            self.assertEqual(res_on.headers.get("X-Exclude-Single-Card-Series"), "1")

            database.set_system_setting("exclude_single_card_series", False)
            res_off = self.app.get("/api/library/profile/portal_child", environ_base={"REMOTE_ADDR": "192.168.1.100"})
            self.assertEqual(res_off.status_code, 200)
            self.assertEqual(res_off.headers.get("X-Exclude-Single-Card-Series"), "0")

            res_portal = self.app.get("/", environ_base={"REMOTE_ADDR": "192.168.1.100"})
            self.assertEqual(res_portal.status_code, 200)
            self.assertIn(b"series_utils.js", res_portal.data)
            self.assertIn(b"librarySortSelect", res_portal.data)

            with self.app.session_transaction() as sess:
                sess["user_id"] = "test_admin_ui"
                sess["username"] = "test_admin_ui"
                sess["role"] = "admin"
            res_admin = self.app.get("/admin")
            self.assertEqual(res_admin.status_code, 200)
            self.assertIn(b"series_utils.js", res_admin.data)
            self.assertIn(b"curationSortSelect", res_admin.data)
        finally:
            if old_val is None:
                with database.get_db_connection() as conn:
                    conn.execute("DELETE FROM system_settings WHERE key = 'exclude_single_card_series';")
                    conn.commit()
            else:
                database.set_system_setting("exclude_single_card_series", old_val)

    def test_kiosk_recently_played_profile_and_device_scope(self):
        """Verifies POST /api/play records profile & device:<id> history, and /api/kids/__all__ scopes Recently Played per player."""
        conn = database.get_db_connection()
        try:
            with conn:
                conn.execute(
                    "INSERT OR REPLACE INTO media_items (id, type, title, author, raw_metadata) VALUES (?, ?, ?, ?, ?);",
                    ("kiosk_book_a", "abs", "Kiosk Recent Book A", "Author A", "{}"),
                )
                conn.execute(
                    "INSERT OR REPLACE INTO media_items (id, type, title, author, raw_metadata) VALUES (?, ?, ?, ?, ?);",
                    ("kiosk_card_b", "yoto", "Kiosk Recent Card B", "Yoto", "{}"),
                )
                conn.execute(
                    "INSERT OR REPLACE INTO profile_media_permissions (profile_id, media_id, permission) VALUES (?, ?, 'allowed');",
                    ("portal_child", "kiosk_book_a"),
                )
                conn.execute(
                    "INSERT OR REPLACE INTO profile_media_permissions (profile_id, media_id, permission) VALUES (?, ?, 'allowed');",
                    ("portal_child", "kiosk_card_b"),
                )
                conn.execute("DELETE FROM media_playback_history WHERE media_id IN ('kiosk_book_a', 'kiosk_card_b');")
        finally:
            conn.close()

        with self.app.session_transaction() as sess:
            sess["user_id"] = "test_admin_kiosk"
            sess["username"] = "test_admin_kiosk"
            sess["role"] = "admin"

        mock_mgr = MagicMock()
        mock_mgr.update_profile_primary_card.return_value = {
            "slot_id": "profile_portal_child",
            "mode": "streaming",
            "yoto_card_id": "yoto_slot_1",
            "title": "Kiosk Recent Book A",
            "tracks": [{"title": "Chapter 1"}],
        }
        mock_yoto = MagicMock()
        mock_yoto.save_stream_playlist.return_value = "yoto_slot_1"
        mock_yoto.get_card_detail.return_value = {"content": {"chapters": [{"title": "Track 1", "key": "01"}]}}

        with patch("core.bridge.manager.get_bridge_manager", return_value=mock_mgr), \
             patch("core.bridge.icons.upload_abs_cover_to_yoto", return_value=""), \
             patch.object(clients, "yoto_client", mock_yoto), \
             patch("routes.esp32._dispatch_card_start", return_value=(True, "", 200)):
            # 1. Play ABS book with profile + bedside player
            res_abs_play = self.app.post("/api/play", json={
                "child": "portal_child",
                "type": "abs",
                "id": "kiosk_book_a",
                "device_id": "player_bedside",
            })
            self.assertEqual(res_abs_play.status_code, 200)

            # 2. Play Yoto card in standalone (no profile) on another player
            res_yoto_play = self.app.post("/api/play", json={
                "child": "",
                "type": "yoto",
                "id": "kiosk_card_b",
                "device_id": "player_den",
            })
            self.assertEqual(res_yoto_play.status_code, 200)

        # Profile-scoped catalog sees portal_child play
        res_prof = self.app.get("/api/kids/portal_child/abs?client=kiosk")
        self.assertEqual(res_prof.status_code, 200)
        prof_items = {x["id"]: x for x in res_prof.get_json()}
        self.assertTrue(prof_items["kiosk_book_a"]["last_played_at"])

        # Player-scoped standalone catalog for player_bedside sees kiosk_book_a (not player_den's card)
        res_bedside = self.app.get("/api/kids/__all__/abs?client=kiosk&device_id=player_bedside")
        self.assertEqual(res_bedside.status_code, 200)
        bed_items = {x["id"]: x for x in res_bedside.get_json()}
        self.assertTrue(bed_items["kiosk_book_a"]["last_played_at"])

        # Player-scoped standalone catalog for player_den sees kiosk_card_b, not kiosk_book_a
        res_den_yoto = self.app.get("/api/kids/__all__/yoto?client=kiosk&device_id=player_den")
        res_den_abs = self.app.get("/api/kids/__all__/abs?client=kiosk&device_id=player_den")
        self.assertEqual(res_den_yoto.status_code, 200)
        self.assertEqual(res_den_abs.status_code, 200)
        den_yoto = {x["id"]: x for x in res_den_yoto.get_json()}
        den_abs = {x["id"]: x for x in res_den_abs.get_json()}
        self.assertTrue(den_yoto["kiosk_card_b"]["last_played_at"])
        self.assertEqual(den_abs["kiosk_book_a"]["last_played_at"], "")


if __name__ == "__main__":
    unittest.main()
