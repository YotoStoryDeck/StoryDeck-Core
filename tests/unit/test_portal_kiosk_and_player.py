"""Unit tests for Parent Portal, Tablet Kiosk assets/catalog, Player service, and YotoClient API mocks."""
from html.parser import HTMLParser
import io
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import MagicMock, patch

from PIL import Image

from tests.harness import IsolatedTestCase
from app import app
import clients
from clients.yoto_client import YotoClient
import core.player_service as player_service
import database


class _PasswordFormChecker(HTMLParser):
    def __init__(self):
        super().__init__()
        self.form_depth = 0
        self.orphan_passwords = []

    def handle_starttag(self, tag, attrs):
        attr_map = {k.lower(): (v or "").lower() for k, v in attrs}
        if tag.lower() == "form":
            self.form_depth += 1
        elif tag.lower() == "input" and attr_map.get("type") == "password":
            if self.form_depth == 0:
                self.orphan_passwords.append(attr_map.get("id") or attr_map.get("name") or "<unnamed>")

    def handle_endtag(self, tag):
        if tag.lower() == "form" and self.form_depth > 0:
            self.form_depth -= 1


class TestPortalKioskAndPlayer(IsolatedTestCase):
    def setUp(self):
        super().setUp()
        self.app = app.test_client()
        database.upsert_profile("portal_child", {
            "display_name": "Portal Child",
            "default_playback_mode": "streaming",
            "offline_retention_limit": 8,
        })

    def test_01_root_portal_serves_html_and_yoto_lockout_ui(self):
        """Verifies GET / serves portal.html with LAN auth and includes Yoto lockout UI."""
        res = self.app.get("/", environ_base={"REMOTE_ADDR": "192.168.1.100"})
        self.assertEqual(res.status_code, 200)
        for token in (
            b"StoryDeck \xe2\x80\xa2 Parent Portal",
            b"Target Yoto Player",
            b"Physical Cards Inventory",
            b"yotoAuthStatusPill",
            b"yotoAuthBlockedView",
            b"Yoto API Token Not Resolving",
            b"btnRetryYotoAuth",
        ):
            self.assertIn(token, res.data)

    def test_02_kiosk_self_hosted_font_and_zero_cdn_links(self):
        """Verifies /kiosk/ serves self-contained static HTML without Google Fonts CDN and serves fredoka-latin.woff2."""
        res_html = self.app.get("/kiosk/")
        self.assertEqual(res_html.status_code, 200)
        html_txt = res_html.get_data(as_text=True)
        self.assertNotIn("fonts.googleapis.com", html_txt)
        self.assertIn("/kiosk/css/main.css", html_txt)

        res_font = self.app.get("/kiosk/fonts/fredoka-latin.woff2")
        self.assertEqual(res_font.status_code, 200)
        self.assertTrue(res_font.data.startswith(b"wOF2"))
        res_font.close()

        res_manifest = self.app.get("/kiosk/manifest.json")
        self.assertEqual(res_manifest.status_code, 200)
        self.assertEqual(res_manifest.get_json().get("orientation"), "any")

    def test_03_player_devices_transport_and_sleep_timer_service(self):
        """Verifies /api/player/devices, /transport, /control, and real sleep_timer service logic."""
        mock_map = {"yoto_123": {"id": "yoto_123", "deviceId": "yoto_123", "name": "Bedroom Yoto", "online": True, "status": "idle"}}
        with patch("core.player_service.get_live_players_map", return_value=mock_map):
            res = self.app.get("/api/player/devices", environ_base={"REMOTE_ADDR": "192.168.1.100"})
            self.assertEqual(res.status_code, 200)
            self.assertEqual(res.get_json()[0]["name"], "Bedroom Yoto")

        with patch("core.player_service.dispatch_player_control", return_value=(True, {"ok": True}, 200)) as mock_ctl:
            self.assertEqual(self.app.post("/api/player/transport", json={"device_id": "yoto_123", "action": "pause"}, environ_base={"REMOTE_ADDR": "192.168.1.100"}).status_code, 200)
            mock_ctl.assert_called_with("yoto_123", "pause", volume=None, minutes=None, position=None, speed=None)

            self.assertEqual(self.app.post("/api/player/transport", json={"device_id": "yoto_123", "action": "volume", "volume": 65}, environ_base={"REMOTE_ADDR": "192.168.1.100"}).status_code, 200)
            mock_ctl.assert_called_with("yoto_123", "volume", volume=65, minutes=None, position=None, speed=None)

        # Real dispatch_player_control for sleep_timer & speed (zero network)
        ok_tm, tm_res, code_tm = player_service.dispatch_player_control("yoto_123", "sleep_timer", minutes=25)
        self.assertTrue(ok_tm)
        self.assertEqual(code_tm, 200)
        self.assertTrue(tm_res["active"])
        self.assertGreater(player_service.get_player_sleep_timer_remaining("yoto_123"), 1400)

        ok_cancel, cancel_res, _ = player_service.dispatch_player_control("yoto_123", "sleep_timer", minutes=0)
        self.assertTrue(ok_cancel)
        self.assertFalse(cancel_res["active"])
        self.assertEqual(player_service.get_player_sleep_timer_remaining("yoto_123"), 0)

    def test_04_library_profile_and_series_utils_headers(self):
        """Verifies GET /api/library/profile/<id> and X-Exclude-Single-Card-Series header parity."""
        database.upsert_media_items([
            {"id": "portal_abs_1", "type": "abs", "title": "Portal ABS Book", "author": "Author A", "raw_metadata": {}},
            {"id": "yotoC1", "type": "yoto", "title": "Portal Yoto Card", "author": "Yoto", "raw_metadata": {}},
        ])
        database.batch_update_profile_media_permissions(["portal_abs_1", "yotoC1"], {"portal_child": "allow"})

        old_val = database.get_system_settings().get("exclude_single_card_series")
        try:
            database.set_system_setting("exclude_single_card_series", True)
            res_on = self.app.get("/api/library/profile/portal_child", environ_base={"REMOTE_ADDR": "192.168.1.100"})
            self.assertEqual(res_on.status_code, 200)
            self.assertEqual(res_on.headers.get("X-Exclude-Single-Card-Series"), "1")
            self.assertEqual({it.get("type") for it in res_on.get_json()}, {"abs", "yoto"})

            database.set_system_setting("exclude_single_card_series", False)
            res_off = self.app.get("/api/library/profile/portal_child", environ_base={"REMOTE_ADDR": "192.168.1.100"})
            self.assertEqual(res_off.headers.get("X-Exclude-Single-Card-Series"), "0")

            res_portal = self.app.get("/", environ_base={"REMOTE_ADDR": "192.168.1.100"})
            self.assertIn(b"series_utils.js", res_portal.data)
            self.assertIn(b"librarySortSelect", res_portal.data)

            with self.app.session_transaction() as sess:
                sess["user_id"] = "test_admin_ui"
                sess["username"] = "test_admin_ui"
                sess["role"] = "admin"
            res_admin = self.app.get("/admin")
            self.assertIn(b"series_utils.js", res_admin.data)
            self.assertIn(b"curationSortSelect", res_admin.data)
        finally:
            if old_val is not None:
                database.set_system_setting("exclude_single_card_series", old_val)

    def test_05_yoto_auth_status_cache_sync_and_concurrent_refresh(self):
        """Verifies /api/yoto/auth-status negative cache, SQLite refresh sync, and thread-safe single refresh."""
        yc = YotoClient(client_id="cid_test", refresh_token="bad_ref")
        mock_bad = MagicMock(status_code=400, text='{"error":"invalid_grant"}')
        mock_bad.json.return_value = {"error": "invalid_grant", "error_description": "Unknown or invalid refresh token."}

        with patch.object(clients, "yoto_client", yc), patch("requests.post", return_value=mock_bad) as mock_post:
            d1 = self.app.get("/api/yoto/auth-status", environ_base={"REMOTE_ADDR": "192.168.1.100"}).get_json()
            self.assertFalse(d1["token_valid"])
            self.assertEqual(d1["reason"], "invalid_grant")
            self.app.get("/api/yoto/auth-status", environ_base={"REMOTE_ADDR": "192.168.1.100"})
            self.assertEqual(mock_post.call_count, 1)
            self.app.get("/api/yoto/auth-status?force=1", environ_base={"REMOTE_ADDR": "192.168.1.100"})
            self.assertEqual(mock_post.call_count, 2)

        yc_sync = YotoClient(client_id="cid_sync", refresh_token="")
        mock_ok = MagicMock(status_code=200)
        mock_ok.json.return_value = {"access_token": "tok_synced", "refresh_token": "rot_ref_synced", "expires_in": 3600}
        database.update_api_credentials({"yoto_client_id": "cid_sync", "yoto_refresh_token": "db_ref_1"})
        with patch.object(clients, "yoto_client", yc_sync), patch("requests.post", return_value=mock_ok):
            res_sync = self.app.get("/api/yoto/auth-status", environ_base={"REMOTE_ADDR": "192.168.1.100"}).get_json()
            self.assertTrue(res_sync["token_valid"])
            self.assertEqual(database.get_api_credentials().get("yoto_refresh_token"), "rot_ref_synced")

        # Concurrent single refresh
        yc_conc = YotoClient(client_id="cid_concurrent", refresh_token="ref_conc_1")
        mock_conc = MagicMock(status_code=200)
        mock_conc.json.return_value = {"access_token": "shared_access_tok", "refresh_token": "ref_conc_2", "expires_in": 3600}

        def slow_post(*args, **kwargs):
            time.sleep(0.02)
            return mock_conc

        results, errors = [], []
        barrier = threading.Barrier(6)

        def worker():
            try:
                barrier.wait(timeout=2)
                results.append(yc_conc.get_token())
            except Exception as exc:
                errors.append(exc)

        with patch("requests.post", side_effect=slow_post) as mock_post, patch("database.update_api_credentials", return_value=True):
            threads = [threading.Thread(target=worker) for _ in range(6)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
        self.assertEqual(errors, [])
        self.assertEqual(mock_post.call_count, 1)
        self.assertEqual(results, ["shared_access_tok"] * 6)

    def test_06_kiosk_recently_played_combined_catalog_and_sleep_timer(self):
        """Verifies /api/play history recording, __all__ device scope, and /api/kids/<child>/catalog."""
        database.upsert_media_items([
            {"id": "kiosk_book_a", "type": "abs", "title": "Kiosk Recent Book A", "author": "Author A", "raw_metadata": {}},
            {"id": "kiosk_card_b", "type": "yoto", "title": "Kiosk Recent Card B", "author": "Yoto", "raw_metadata": {}},
        ])
        database.batch_update_profile_media_permissions(["kiosk_book_a", "kiosk_card_b"], {"portal_child": "allow"})

        with self.app.session_transaction() as sess:
            sess["user_id"] = "test_admin_kiosk"
            sess["username"] = "test_admin_kiosk"
            sess["role"] = "admin"

        mock_mgr = MagicMock()
        mock_mgr.update_profile_primary_card.return_value = {"slot_id": "profile_portal_child", "mode": "streaming", "yoto_card_id": "yoto_slot_1", "title": "Kiosk Recent Book A", "tracks": [{"title": "Chapter 1"}]}
        mock_yoto = MagicMock()
        mock_yoto.save_stream_playlist.return_value = "yoto_slot_1"
        mock_yoto.get_card_detail.return_value = {"content": {"chapters": [{"title": "Track 1", "key": "01"}]}}

        with patch("core.bridge.manager.get_bridge_manager", return_value=mock_mgr), \
             patch("core.bridge.icons.upload_abs_cover_to_yoto", return_value=""), \
             patch.object(clients, "yoto_client", mock_yoto), \
             patch("routes.esp32._dispatch_card_start", return_value=(True, "", 200)):
            self.assertEqual(self.app.post("/api/play", json={"child": "portal_child", "type": "abs", "id": "kiosk_book_a", "device_id": "player_bedside"}).status_code, 200)
            self.assertEqual(self.app.post("/api/play", json={"child": "", "type": "yoto", "id": "kiosk_card_b", "device_id": "player_den"}).status_code, 200)

        prof_items = {x["id"]: x for x in self.app.get("/api/kids/portal_child/catalog?client=kiosk").get_json()["abs"]}
        self.assertTrue(prof_items["kiosk_book_a"]["last_played_at"])

        bed_data = self.app.get("/api/kids/__all__/catalog?client=kiosk&device_id=player_bedside").get_json()
        self.assertTrue({x["id"]: x for x in bed_data["abs"]}["kiosk_book_a"]["last_played_at"])

        den_abs = {x["id"]: x for x in self.app.get("/api/kids/__all__/abs?client=kiosk&device_id=player_den").get_json()}
        den_yoto = {x["id"]: x for x in self.app.get("/api/kids/__all__/yoto?client=kiosk&device_id=player_den").get_json()}
        self.assertTrue(den_yoto["kiosk_card_b"]["last_played_at"])
        self.assertEqual(den_abs["kiosk_book_a"]["last_played_at"], "")

        with patch("core.player_service.dispatch_player_control", return_value=(True, {"ok": True, "minutes": 30}, 200)) as mock_ctl:
            self.assertEqual(self.app.post("/api/player/control", json={"device_id": "player_bedside", "action": "sleep_timer", "minutes": 30}).status_code, 200)
            mock_ctl.assert_called_once_with("player_bedside", "sleep_timer", volume=None, minutes=30, position=None, speed=None)

    def test_07_removed_legacy_routes_health_discovery_play_and_yoto_client(self):
        """Verifies legacy route removal, /health standalone flags, ABS discovery, native /api/play, and YotoClient APIs."""
        with self.app.session_transaction() as sess:
            sess["user_id"] = "admin_user"
            sess["username"] = "admin"
            sess["role"] = "admin"

        for method, path in [("POST", "/api/admin/test/bridge"), ("GET", "/api/admin/yoto/cards"), ("POST", "/api/admin/devices/aa:bb:cc:dd:ee:ff/reset-pin"), ("GET", "/simulator"), ("GET", "/simulator/")]:
            self.assertEqual(self.app.open(path, method=method).status_code, 404)

        orig_creds = database.get_api_credentials()
        try:
            database.update_api_credentials({"abs_base_url": "http://abs.local:13378", "abs_api_key": "abs_key_123456", "yoto_client_id": "yoto_cid_123", "yoto_refresh_token": "yoto_rt_123"})
            h_data = self.app.get("/health").get_json()
            self.assertEqual(h_data["connection_mode"], "standalone")
            self.assertTrue(h_data["features"]["embedded_bridge"])
        finally:
            database.update_api_credentials(orig_creds)

        mock_abs = MagicMock(base_url="http://abs.local:13378")
        mock_abs.get_headers.return_value = {"Authorization": "Bearer test_tok"}
        mock_resp = MagicMock(status_code=200)
        mock_resp.json.return_value = {"users": [{"id": "u1", "username": "alice", "type": "user"}, {"id": "u2", "username": "parent", "type": "root"}]}
        with patch.object(clients, "abs_client", mock_abs), patch("routes.admin_discovery.requests.get", return_value=mock_resp):
            users = self.app.get("/api/admin/discovery/abs-users").get_json()
            self.assertFalse(users[0]["is_admin"])
            self.assertTrue(users[1]["is_admin"])

        database.upsert_profile("bug1tester", {"display_name": "Bug1Tester", "default_player_id": "yoto_player_b1", "player_ids": ["yoto_player_b1"]})
        mock_mgr = MagicMock()
        mock_mgr.update_profile_primary_card.return_value = {"slot_id": "profile_bug1tester", "yoto_card_id": "card_slot_b1", "mode": "streaming", "title": "The Borrowers", "tracks": [{"title": "Chapter 1"}, {"title": "Chapter 2"}]}
        mock_mqtt = MagicMock(is_connected=True)
        mock_mqtt.play.return_value = True
        mock_yoto = MagicMock()
        mock_yoto.save_stream_playlist.return_value = "card_slot_b1"
        with patch("core.bridge.manager.get_bridge_manager", return_value=mock_mgr), \
             patch("core.bridge.mqtt_client.get_mqtt_bridge", return_value=mock_mqtt), \
             patch("core.bridge.playback.get_playback_manager", return_value=MagicMock()), \
             patch.object(clients, "yoto_client", mock_yoto):
            payload = self.app.post("/api/play", json={"child": "bug1tester", "type": "abs", "id": "abs_item_b1", "chapter": 2}).get_json()
            self.assertEqual(payload["chapter"], 2)
            self.assertEqual(payload["chapter_title"], "Chapter 2")
            mock_mqtt.play.assert_called_once_with("yoto_player_b1", "card_slot_b1", track=2)

        # YotoClient devices, raw library normalization, card detail fallback & update
        yc = YotoClient(client_id="cid_dev_test", refresh_token="rt_dev_test")
        resp_401 = MagicMock(status_code=401, text="expired")
        resp_200 = MagicMock(status_code=200)
        resp_200.json.return_value = {"devices": [{"deviceId": "dev_a", "name": "Child B "}, {"deviceId": "dev_b", "name": "  Child A Mini  "}]}
        with patch.object(yc, "get_token", side_effect=["tok_old", "tok_fresh"]) as mock_tok, patch("clients.yoto.library.requests.get", side_effect=[resp_401, resp_200]):
            devs = yc.get_devices()
        self.assertEqual([d["name"] for d in devs], ["Child B", "Child A Mini"])
        mock_tok.assert_any_call(force_refresh=True)

        # Password form check in /admin
        parser = _PasswordFormChecker()
        parser.feed(self.app.get("/admin").get_data(as_text=True))
        self.assertEqual(parser.orphan_passwords, [])


if __name__ == "__main__":
    unittest.main()
