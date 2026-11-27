"""Unit tests for Bridge card CRUD, physical MYO pairing, playlists, thumbnails, and split-track custom icons."""
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from PIL import Image

from tests.harness import IsolatedTestCase
from app import app
import clients
from core.bridge.icons import (
    _UPLOADED_ICON_CACHE,
    get_or_upload_yoto_icon,
    render_track_icon_png,
    resolve_source_chapter_idx,
)
from core.bridge.manager import get_bridge_manager
from core.image_resizer import get_thumbnail, invalidate_thumbnail
import database
import routes.bridge_cards as bridge_cards_module


class TestBridgeCardsPlaylistsAndIcons(IsolatedTestCase):
    def setUp(self):
        super().setUp()
        self.client = app.test_client()
        database.upsert_profile("child_a", {"display_name": "Child A", "default_playback_mode": "streaming"})
        with self.client.session_transaction() as sess:
            sess["user_id"] = "pair_admin"
            sess["username"] = "pair_admin"
            sess["role"] = "admin"

    def test_01_bridge_manager_slot_crud_and_api_cards(self):
        """Verifies slot CRUD, speed clamping, own_place, pin toggle, mode override, and /api/cards REST."""
        mgr = get_bridge_manager()
        with patch.object(clients.abs_client, "get_item", return_value=None):
            slot = mgr.assign_abs_book_to_slot(
                slot_id="fleet_dino",
                abs_item_id="item_123",
                title="Dinosaur Adventures",
                author="Author Dan",
                kind="permanent_myo",
                mode="streaming",
            )
            self.assertEqual(slot["slot_id"], "fleet_dino")

            mgr.set_slot_speed("fleet_dino", 3.5)
            self.assertEqual(mgr.get_slot("fleet_dino")["speed"], 2.0)
            mgr.set_slot_speed("fleet_dino", 0.1)
            self.assertEqual(mgr.get_slot("fleet_dino")["speed"], 0.5)

            mgr.set_slot_own_place("fleet_dino", True)
            mgr.set_slot_pin("fleet_dino", True)
            s = mgr.get_slot("fleet_dino")
            self.assertEqual(s["own_place"], 1)
            self.assertEqual(s["is_pinned"], 1)

            slot_stream = mgr.update_profile_primary_card(profile_id="child_a", abs_item_id="abs_b1", mode_override="default")
            self.assertEqual(slot_stream["slot_id"], "profile_child_a")
            self.assertEqual(slot_stream["mode"], "streaming")

            slot_off = mgr.update_profile_primary_card(profile_id="child_a", abs_item_id="abs_b2", mode_override="offline")
            self.assertEqual(slot_off["mode"], "offline")

            mgr.assign_abs_book_to_slot(slot_id="slot_mobile", abs_item_id="item_abc", title="Mobile Book", author="Jane Doe")

        res_list = self.client.get("/api/cards")
        self.assertEqual(res_list.status_code, 200)
        self.assertTrue(any(x["slot_id"] == "slot_mobile" for x in res_list.get_json()))

        res_put = self.client.put("/api/cards/slot_mobile", json={"speed": 1.25, "is_pinned": True})
        self.assertEqual(res_put.status_code, 200)
        self.assertEqual(res_put.get_json()["speed"], 1.25)

        self.assertEqual(self.client.delete("/api/cards/slot_mobile").status_code, 200)
        self.assertIsNone(mgr.get_slot("slot_mobile"))

    def test_02_yoto_playlists_classification_and_deduplication(self):
        """Tests /api/yoto/playlists enrichment and broker card deduplication in get_raw_library."""
        mgr = get_bridge_manager()
        with patch.object(clients.abs_client, "get_item", return_value=None):
            mgr.assign_abs_book_to_slot(slot_id="broker_slot_1", abs_item_id="item_xyz", title="StoryDeck Audiobook")
            mgr.set_slot_pin("broker_slot_1", True)
            mgr.assign_abs_book_to_slot(slot_id="profile_child_a", abs_item_id="book_123", title="Profile Active Book")

        with database.get_db_connection() as conn:
            conn.execute("UPDATE bridge_slots SET yoto_card_id = 'e5btM' WHERE slot_id = 'profile_child_a';")
            conn.commit()

        mock_playlists = [
            {"cardId": "broker_slot_1", "title": "StoryDeck Audiobook", "metadata": {"description": "Streams from Audiobookshelf via StoryDeck Core"}},
            {"cardId": "custom_yoto_card", "title": "Bedtime Lullabies", "shareType": "myo", "metadata": {"description": "Recorded songs from grandma"}},
        ]
        with patch.object(clients.yoto_client, "get_all_cloud_playlists", return_value=mock_playlists):
            res = self.client.get("/api/yoto/playlists")
            self.assertEqual(res.status_code, 200)
            items = res.get_json()
            broker_item = next(x for x in items if x["card_id"] == "broker_slot_1")
            custom_item = next(x for x in items if x["card_id"] == "custom_yoto_card")
            self.assertEqual(broker_item["source"], "broker")
            self.assertTrue(broker_item["is_pinned"])
            self.assertEqual(custom_item["source"], "custom")
            self.assertTrue(custom_item["is_physical"])

        mock_library = [
            {"cardId": "profile_child_a", "title": "Profile Active Book", "metadata": {"generator": "storydeck-core"}},
            {"cardId": "e5btM", "title": "Profile Active Book", "isMyo": True, "metadata": {}},
            {"cardId": "retail_card_999", "title": "Disney Frozen", "metadata": {"author": "Disney"}},
        ]
        with patch.object(clients.yoto_client, "load_local_family_library", return_value=[]), \
             patch.object(clients.yoto_client, "get_token", return_value="fake_token"), \
             patch("requests.get") as mock_get:
            mock_res = MagicMock(status_code=200)
            mock_res.json.return_value = {"cards": mock_library}
            mock_get.return_value = mock_res
            card_ids = [c.get("cardId") for c in clients.yoto_client.get_raw_library()]
        self.assertIn("retail_card_999", card_ids)
        self.assertNotIn("profile_child_a", card_ids)
        self.assertNotIn("e5btM", card_ids)

    def test_03_card_detection_and_pairing(self):
        """Tests /api/cards/detect (empty, editable MYO, locked retail) and /api/cards/pair."""
        with patch("core.player_service.get_player_status", return_value={"online": True, "card_id": ""}):
            res = self.client.get("/api/cards/detect?device_id=child-a-mini")
            self.assertEqual(res.status_code, 200)
            self.assertFalse(res.get_json()["detected"])

        fake_yoto = MagicMock()
        fake_yoto.get_all_cloud_playlists.return_value = [{"cardId": "myo123", "title": "My MYO"}]
        fake_yoto.get_card_detail.return_value = {"title": "My MYO", "userId": "user_abc", "isMyo": True}
        with patch("core.player_service.get_player_status", return_value={"online": True, "card_id": "myo123", "title": "My MYO"}), \
             patch.object(bridge_cards_module.clients, "yoto_client", fake_yoto):
            data = self.client.get("/api/cards/detect?device_id=child-a-mini").get_json()
            self.assertTrue(data["detected"])
            self.assertTrue(data["is_editable"])
            self.assertFalse(data["is_locked"])

        fake_yoto.get_all_cloud_playlists.return_value = []
        fake_yoto.get_card_detail.return_value = {"title": "Charlie Brown", "userId": "yoto", "isMyo": False}
        with patch("core.player_service.get_player_status", return_value={"online": True, "card_id": "8KkQc", "title": "Charlie Brown"}), \
             patch.object(bridge_cards_module.clients, "yoto_client", fake_yoto):
            data = self.client.get("/api/cards/detect?device_id=child-a-mini").get_json()
            self.assertTrue(data["is_locked"])

        res_prof = self.client.post("/api/cards/pair", json={"card_id": "myo_child_a_01", "target": "profile", "profile_id": "child_a", "name": "Child A Primary"})
        self.assertEqual(res_prof.status_code, 200)
        self.assertEqual(database.get_profile_by_id("child_a").get("primary_card_id"), "myo_child_a_01")

        res_fleet = self.client.post("/api/cards/pair", json={"card_id": "myo_fleet_01", "target": "fleet", "name": "Car Trip Card"})
        self.assertEqual(res_fleet.status_code, 201)

    def test_04_thumbnail_invalidation_and_custom_split_track_icons(self):
        """Tests thumbnail cache invalidation, resolve_source_chapter_idx(), digest deduplication, and /s/icon split routing."""
        buf = io.BytesIO()
        Image.new("RGB", (10, 10), color="red").save(buf, format="PNG")
        thumb_path = get_thumbnail("test_invalidate_cache_key", lambda: buf.getvalue(), size=100, crop=True)
        self.assertTrue(thumb_path.exists())
        invalidate_thumbnail("test_invalidate_cache_key", size=100)
        self.assertFalse(thumb_path.exists())

        chapters = [
            {"start": 0.0, "end": 1200.0, "title": "Chapter 1"},
            {"start": 1200.0, "end": 6600.0, "title": "Chapter 2 (Long)"},
            {"start": 6600.0, "end": 7800.0, "title": "Chapter 3"},
        ]
        self.assertEqual(resolve_source_chapter_idx(chapters, 1200.0, 3000.0), 1)
        self.assertEqual(resolve_source_chapter_idx(chapters, 3000.0, 4800.0), 1)
        self.assertEqual(resolve_source_chapter_idx(chapters, 4800.0, 6600.0), 1)
        self.assertEqual(resolve_source_chapter_idx(chapters, 6600.0, 7800.0), 2)
        self.assertEqual(resolve_source_chapter_idx([], 1800.0, 3600.0), 0)

        ch1_png = b"\x89PNG\r\n\x1a\ncustom_chapter_2_icon"
        book2_png = b"\x89PNG\r\n\x1a\ncustom_book2_chapter_1_icon"
        _UPLOADED_ICON_CACHE.clear()
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            (tmp_path / "book_split_1.png").write_bytes(ch1_png)
            (tmp_path / "book_series2_0.png").write_bytes(book2_png)

            with patch("core.bridge.icons.CUSTOM_ICONS_DIR", tmp_path), \
                 patch.object(clients.yoto_client, "upload_display_icon", return_value="yoto:#customCh2Ref") as mock_up:
                self.assertEqual(render_track_icon_png("book_split", track_idx=2, chapter_idx=1), ch1_png)
                self.assertEqual(render_track_icon_png("book_split", track_idx=3, chapter_idx=1), ch1_png)
                self.assertEqual(get_or_upload_yoto_icon("book_split", track_idx=2, chapter_idx=1), "yoto:#customCh2Ref")
                self.assertEqual(get_or_upload_yoto_icon("book_split", track_idx=3, chapter_idx=1), "yoto:#customCh2Ref")
                mock_up.assert_called_once()
                self.assertEqual(render_track_icon_png("book_series2", track_idx=16, chapter_idx=0), book2_png)

        ch0_png = b"\x89PNG\r\n\x1a\nsplit_ch1_custom_art"
        with database.get_db_connection() as conn:
            conn.execute("INSERT INTO bridge_slots (slot_id, abs_item_id, title, kind, mode, total_duration) VALUES ('slot_split_icon_test', 'book_split_stream', 'Split Book', 'permanent_myo', 'streaming', 5400.0);")
            conn.execute("INSERT INTO bridge_tracks (slot_id, track_idx, abs_item_id, book_title, book_duration, title, start_time, end_time, duration, stream_url) VALUES ('slot_split_icon_test', 2, 'book_split_stream', 'Split Book', 5400.0, 'Chapter 1 (2/2)', 1800.0, 3600.0, 1800.0, '/s/audio/slot_split_icon_test/2');")
            conn.commit()

        fake_item = {"media": {"chapters": [{"start": 0.0, "end": 3600.0, "title": "Chapter 1"}, {"start": 3600.0, "end": 5400.0, "title": "Chapter 2"}]}}
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            (tmp_path / "book_split_stream_0.png").write_bytes(ch0_png)
            with patch("core.bridge.icons.CUSTOM_ICONS_DIR", tmp_path), \
                 patch.object(clients.abs_client, "get_item", return_value=fake_item):
                res = self.client.get("/s/icon/slot_split_icon_test/2.png")
                self.assertEqual(res.status_code, 200)
                self.assertEqual(res.data, ch0_png)


if __name__ == "__main__":
    unittest.main()
